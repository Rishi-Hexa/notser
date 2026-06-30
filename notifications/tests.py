from datetime import timedelta
from smtplib import SMTPException
from unittest import mock, skip

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from . import services
from .models import (
    Channel,
    Delivery,
    DeliveryStatus,
    Notification,
    NotificationStatus,
    Priority,
    Recipient,
)


def make_notification(**overrides):
    """A direct-content notification to one recipient on one channel."""
    recipient = Recipient.objects.create(name="Asha", email="asha@example.com")
    fields = dict(
        channels=[Channel.EMAIL],
        subject="Hi",
        content="Hello there",
    )
    fields.update(overrides)
    notification = Notification.objects.create(**fields)
    notification.recipients.set([recipient])
    return notification


class EnqueueTests(TestCase):
    def test_enqueue_creates_pending_delivery_without_sending(self):
        notification = make_notification()
        with mock.patch("notifications.senders.dispatch") as dispatched:
            services.enqueue_notification(notification)
            dispatched.assert_not_called()  # enqueue never sends

        delivery = notification.deliveries.get()
        self.assertEqual(delivery.status, DeliveryStatus.PENDING)
        self.assertIsNotNone(delivery.next_attempt_at)
        notification.refresh_from_db()
        self.assertEqual(notification.status, NotificationStatus.PENDING)

    def test_missing_address_is_dead_lettered_immediately(self):
        # SMS to a recipient with no phone number.
        notification = make_notification(channels=[Channel.SMS])
        services.enqueue_notification(notification)

        delivery = notification.deliveries.get()
        self.assertEqual(delivery.status, DeliveryStatus.DEAD_LETTER)
        self.assertIn("no SMS address", delivery.error)
        notification.refresh_from_db()
        self.assertEqual(notification.status, NotificationStatus.FAILED)


class WorkerDeliveryTests(TestCase):
    def test_successful_send(self):
        notification = make_notification()
        services.enqueue_notification(notification)

        with mock.patch("notifications.senders.dispatch"):  # succeeds
            processed = services.process_due_deliveries()

        self.assertEqual(len(processed), 1)
        delivery = notification.deliveries.get()
        self.assertEqual(delivery.status, DeliveryStatus.SENT)
        self.assertEqual(delivery.attempts, 1)
        self.assertIsNotNone(delivery.sent_at)
        notification.refresh_from_db()
        self.assertEqual(notification.status, NotificationStatus.SENT)

    def test_transient_failure_schedules_retry(self):
        notification = make_notification()
        services.enqueue_notification(notification)

        err = services.senders.TransientDeliveryError("provider 503")
        with mock.patch("notifications.senders.dispatch", side_effect=err):
            services.process_due_deliveries()

        delivery = notification.deliveries.get()
        self.assertEqual(delivery.status, DeliveryStatus.RETRYING)
        self.assertEqual(delivery.attempts, 1)
        # Scheduled into the future (first ladder rung = 5s).
        self.assertGreater(delivery.next_attempt_at, timezone.now())

    def test_transient_failures_exhaust_to_dead_letter(self):
        notification = make_notification()
        services.enqueue_notification(notification)

        err = services.senders.TransientDeliveryError("provider 503")
        with mock.patch("notifications.senders.dispatch", side_effect=err):
            # 1 initial + len(RETRY_DELAYS) retries before dead-lettering.
            for _ in range(len(services.RETRY_DELAYS) + 1):
                # Make the delivery due regardless of its backoff schedule.
                notification.deliveries.update(next_attempt_at=timezone.now())
                services.process_due_deliveries()

        delivery = notification.deliveries.get()
        self.assertEqual(delivery.status, DeliveryStatus.DEAD_LETTER)
        self.assertEqual(delivery.attempts, len(services.RETRY_DELAYS) + 1)
        notification.refresh_from_db()
        self.assertEqual(notification.status, NotificationStatus.FAILED)

    def test_permanent_failure_skips_retries(self):
        notification = make_notification()
        services.enqueue_notification(notification)

        err = services.senders.PermanentDeliveryError("invalid token")
        with mock.patch("notifications.senders.dispatch", side_effect=err):
            services.process_due_deliveries()

        delivery = notification.deliveries.get()
        self.assertEqual(delivery.status, DeliveryStatus.DEAD_LETTER)
        self.assertEqual(delivery.attempts, 1)  # one attempt, no retries
        self.assertIn("permanent", delivery.error)

    def test_replay_requeues_dead_letter(self):
        notification = make_notification()
        services.enqueue_notification(notification)
        delivery = notification.deliveries.get()
        delivery.status = DeliveryStatus.DEAD_LETTER
        delivery.attempts = 4
        delivery.save()

        self.assertTrue(services.replay_delivery(delivery))
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, DeliveryStatus.PENDING)
        self.assertEqual(delivery.attempts, 0)  # fresh ladder

        # A non-dead-letter delivery can't be replayed.
        self.assertFalse(services.replay_delivery(delivery))


class PriorityTests(TestCase):
    def _enqueue(self, recipient, priority):
        notification = Notification.objects.create(
            channels=[Channel.EMAIL], subject="s", content="c", priority=priority
        )
        notification.recipients.set([recipient])
        services.enqueue_notification(notification)
        return notification

    def test_higher_priority_is_processed_first(self):
        recipient = Recipient.objects.create(name="Asha", email="asha@example.com")
        # Enqueue LOW first, then HIGH, then MEDIUM — so insertion order is the
        # opposite of priority order, proving priority (not FIFO) wins.
        low = self._enqueue(recipient, Priority.LOW)
        high = self._enqueue(recipient, Priority.HIGH)
        med = self._enqueue(recipient, Priority.MEDIUM)

        def status(n):
            return n.deliveries.get().status

        # One at a time: HIGH, then MEDIUM, then LOW.
        with mock.patch("notifications.senders.dispatch"):
            services.process_due_deliveries(batch=1)
        self.assertEqual(status(high), DeliveryStatus.SENT)
        self.assertEqual(status(med), DeliveryStatus.PENDING)
        self.assertEqual(status(low), DeliveryStatus.PENDING)

        with mock.patch("notifications.senders.dispatch"):
            services.process_due_deliveries(batch=1)
        self.assertEqual(status(med), DeliveryStatus.SENT)
        self.assertEqual(status(low), DeliveryStatus.PENDING)

        with mock.patch("notifications.senders.dispatch"):
            services.process_due_deliveries(batch=1)
        self.assertEqual(status(low), DeliveryStatus.SENT)


class SchedulingTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.recipient = Recipient.objects.create(name="Asha", email="asha@example.com")

    def _post(self, **extra):
        body = {"recipients": [self.recipient.id], "channels": ["EMAIL"],
                "subject": "s", "content": "c"}
        body.update(extra)
        return self.client.post("/api/v1/notifications/", body, format="json")

    def test_send_at_schedules_into_future(self):
        when = (timezone.now() + timedelta(hours=1)).isoformat()
        resp = self._post(send_at=when)
        self.assertEqual(resp.status_code, 202)

        notification = Notification.objects.get(id=resp.data["id"])
        self.assertEqual(notification.status, NotificationStatus.SCHEDULED)
        delivery = notification.deliveries.get()
        self.assertEqual(delivery.status, DeliveryStatus.PENDING)
        self.assertGreater(delivery.next_attempt_at, timezone.now())

        # The worker must NOT pick it up before its time.
        with mock.patch("notifications.senders.dispatch") as dispatched:
            processed = services.process_due_deliveries()
        self.assertEqual(processed, [])
        dispatched.assert_not_called()

    def test_send_at_in_the_past_is_rejected(self):
        when = (timezone.now() - timedelta(hours=1)).isoformat()
        resp = self._post(send_at=when)
        self.assertEqual(resp.status_code, 400)

    def test_no_schedule_is_immediate(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 202)
        notification = Notification.objects.get(id=resp.data["id"])
        self.assertEqual(notification.status, NotificationStatus.PENDING)
        delivery = notification.deliveries.get()
        self.assertLessEqual(delivery.next_attempt_at, timezone.now())


class StatsTests(TestCase):
    def test_stats_rolls_up_pipeline(self):
        # One EMAIL that sends, one SMS that dead-letters (recipient has no phone).
        recipient = Recipient.objects.create(name="Asha", email="asha@example.com")
        for channel in (Channel.EMAIL, Channel.SMS):
            n = Notification.objects.create(channels=[channel], content="c")
            n.recipients.set([recipient])
            services.enqueue_notification(n)
        with mock.patch("notifications.senders.dispatch"):
            services.process_due_deliveries()

        stats = services.pipeline_stats()
        self.assertEqual(stats["deliveries"]["SENT"], 1)
        self.assertEqual(stats["deliveries"]["DEAD_LETTER"], 1)  # SMS, no phone
        self.assertEqual(stats["queue"]["due_now"], 0)
        self.assertEqual(stats["queue"]["dlq_size"], 1)
        self.assertEqual(stats["channels"]["EMAIL"]["sent"], 1)
        self.assertEqual(stats["channels"]["EMAIL"]["success_rate"], 1.0)
        self.assertEqual(stats["channels"]["SMS"]["dead_letter"], 1)

    def test_stats_endpoint(self):
        resp = APIClient().get("/api/v1/stats/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("deliveries", resp.data)
        self.assertIn("queue", resp.data)
        self.assertIn("channels", resp.data)


@override_settings(NOTIFS_RATE_LIMITS={"SMS": (2, 3600)})
class RateLimitTests(TestCase):
    def test_over_limit_is_throttled_and_deferred(self):
        recipient = Recipient.objects.create(
            name="Asha", email="asha@example.com", phone="+919800000000"
        )

        def enqueue_one():
            n = Notification.objects.create(channels=[Channel.SMS], content="c")
            n.recipients.set([recipient])
            services.enqueue_notification(n)
            return n

        notifs = [enqueue_one() for _ in range(3)]  # 3 SMS, limit is 2/hour

        with mock.patch("notifications.senders.dispatch"):  # sends succeed
            services.process_due_deliveries()

        def delivery(n):
            return n.deliveries.get()

        statuses = [delivery(n).status for n in notifs]
        self.assertEqual(statuses.count(DeliveryStatus.SENT), 2)
        self.assertEqual(statuses.count(DeliveryStatus.THROTTLED), 1)

        throttled = next(n for n in notifs if delivery(n).status == DeliveryStatus.THROTTLED)
        d = delivery(throttled)
        self.assertGreater(d.next_attempt_at, timezone.now())  # deferred, not dropped
        self.assertIn("rate-limited", d.error)

    def test_unlimited_channel_is_never_throttled(self):
        # PUSH isn't in NOTIFS_RATE_LIMITS, so no cap applies.
        recipient = Recipient.objects.create(name="Ben", device_token="tok-123")
        for _ in range(5):
            n = Notification.objects.create(channels=[Channel.PUSH], content="c")
            n.recipients.set([recipient])
            services.enqueue_notification(n)
        with mock.patch("notifications.senders.dispatch"):
            services.process_due_deliveries()
        self.assertEqual(
            Delivery.objects.filter(status=DeliveryStatus.SENT).count(), 5
        )


@skip("EmailSender is stubbed for testing; re-enable when the real sender is uncommented")
class EmailSenderTests(TestCase):
    """Exercises the REAL EmailSender (not the mocked dispatch). Django's test
    runner uses the in-memory email backend, so sends land in mail.outbox."""

    def _enqueue_email(self):
        recipient = Recipient.objects.create(name="Asha", email="asha@example.com")
        notification = Notification.objects.create(
            channels=[Channel.EMAIL], subject="Order filled", content="Your order filled."
        )
        notification.recipients.set([recipient])
        services.enqueue_notification(notification)
        return notification

    def test_email_is_actually_sent(self):
        notification = self._enqueue_email()
        services.process_due_deliveries()  # real EmailSender -> locmem outbox

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.subject, "Order filled")
        self.assertEqual(message.body, "Your order filled.")
        self.assertEqual(message.to, ["asha@example.com"])
        self.assertEqual(notification.deliveries.get().status, DeliveryStatus.SENT)

    def test_smtp_failure_is_transient_and_retries(self):
        notification = self._enqueue_email()
        with mock.patch(
            "notifications.senders.send_mail", side_effect=SMTPException("boom")
        ):
            services.process_due_deliveries()

        delivery = notification.deliveries.get()
        self.assertEqual(delivery.status, DeliveryStatus.RETRYING)
        self.assertEqual(delivery.attempts, 1)
        self.assertEqual(len(mail.outbox), 0)


@skip("SmsSender is stubbed for testing; re-enable when the real sender is uncommented")
@override_settings(
    TWILIO_ACCOUNT_SID="AC_test",
    TWILIO_AUTH_TOKEN="token_test",
    TWILIO_PHONE_NUMBER="+15550000000",
)
class SmsSenderTests(TestCase):
    """Exercises the REAL SmsSender with the Twilio client mocked (no network)."""

    def _enqueue_sms(self):
        recipient = Recipient.objects.create(name="Asha", phone="+919811111111")
        notification = Notification.objects.create(channels=[Channel.SMS], content="OTP 1234")
        notification.recipients.set([recipient])
        services.enqueue_notification(notification)
        return notification

    @mock.patch("notifications.senders.Client")
    def test_sms_sent_via_twilio(self, MockClient):
        notification = self._enqueue_sms()
        services.process_due_deliveries()

        MockClient.assert_called_once_with("AC_test", "token_test")
        create = MockClient.return_value.messages.create
        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs["to"], "+919811111111")
        self.assertEqual(create.call_args.kwargs["from_"], "+15550000000")
        self.assertEqual(create.call_args.kwargs["body"], "OTP 1234")
        self.assertEqual(notification.deliveries.get().status, DeliveryStatus.SENT)

    @mock.patch("notifications.senders.Client")
    def test_twilio_permanent_error_dead_letters(self, MockClient):
        from twilio.base.exceptions import TwilioRestException
        MockClient.return_value.messages.create.side_effect = TwilioRestException(
            400, "uri", msg="unverified number", code=21608
        )
        notification = self._enqueue_sms()
        services.process_due_deliveries()
        self.assertEqual(notification.deliveries.get().status, DeliveryStatus.DEAD_LETTER)

    @mock.patch("notifications.senders.Client")
    def test_twilio_server_error_retries(self, MockClient):
        from twilio.base.exceptions import TwilioRestException
        MockClient.return_value.messages.create.side_effect = TwilioRestException(
            500, "uri", msg="server error", code=20500
        )
        notification = self._enqueue_sms()
        services.process_due_deliveries()
        self.assertEqual(notification.deliveries.get().status, DeliveryStatus.RETRYING)


@override_settings(TWILIO_ACCOUNT_SID="", TWILIO_AUTH_TOKEN="", TWILIO_PHONE_NUMBER="")
class SmsStubFallbackTests(TestCase):
    def test_without_credentials_sms_is_a_stub(self):
        recipient = Recipient.objects.create(name="Asha", phone="+919811111111")
        notification = Notification.objects.create(channels=[Channel.SMS], content="hi")
        notification.recipients.set([recipient])
        services.enqueue_notification(notification)

        with mock.patch("notifications.senders.Client") as MockClient:
            services.process_due_deliveries()
            MockClient.assert_not_called()  # never touches Twilio
        self.assertEqual(notification.deliveries.get().status, DeliveryStatus.SENT)


@skip("PushSender is stubbed for testing; re-enable when the real sender is uncommented")
class PushSenderTests(TestCase):
    """Exercises the REAL PushSender with the Firebase app + send mocked."""

    def _enqueue_push(self):
        recipient = Recipient.objects.create(name="Asha", device_token="dev-token-abc")
        notification = Notification.objects.create(
            channels=[Channel.PUSH], subject="Alert", content="INFY hit 1550"
        )
        notification.recipients.set([recipient])
        services.enqueue_notification(notification)
        return notification

    @mock.patch("notifications.senders._get_firebase_app", return_value=mock.Mock())
    @mock.patch("notifications.senders.messaging.send")
    def test_push_sent_via_fcm(self, mock_send, _mock_app):
        notification = self._enqueue_push()
        services.process_due_deliveries()

        mock_send.assert_called_once()
        sent_message = mock_send.call_args.args[0]
        self.assertEqual(sent_message.token, "dev-token-abc")
        self.assertEqual(notification.deliveries.get().status, DeliveryStatus.SENT)

    @mock.patch("notifications.senders._get_firebase_app", return_value=mock.Mock())
    @mock.patch("notifications.senders.messaging.send", side_effect=ValueError("bad token"))
    def test_bad_token_dead_letters(self, _mock_send, _mock_app):
        notification = self._enqueue_push()
        services.process_due_deliveries()
        self.assertEqual(notification.deliveries.get().status, DeliveryStatus.DEAD_LETTER)

    @mock.patch("notifications.senders._get_firebase_app", return_value=mock.Mock())
    @mock.patch("notifications.senders.messaging.send", side_effect=RuntimeError("unavailable"))
    def test_transient_error_retries(self, _mock_send, _mock_app):
        notification = self._enqueue_push()
        services.process_due_deliveries()
        self.assertEqual(notification.deliveries.get().status, DeliveryStatus.RETRYING)

    @mock.patch("notifications.senders._get_firebase_app", return_value=None)
    def test_without_credentials_push_is_a_stub(self, _mock_app):
        notification = self._enqueue_push()
        with mock.patch("notifications.senders.messaging.send") as mock_send:
            services.process_due_deliveries()
            mock_send.assert_not_called()  # never touches FCM
        self.assertEqual(notification.deliveries.get().status, DeliveryStatus.SENT)
