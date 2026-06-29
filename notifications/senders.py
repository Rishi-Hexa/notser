"""Channel senders + dispatcher.

The Python equivalent of the Java NotificationSender / Factory / Dispatcher
reference. Each sender acts on a single Delivery (one recipient, one channel,
with its address + rendered content already resolved). For now they just log —
real provider integrations (SES/Twilio/FCM) swap in behind the same interface.
"""
import logging
import os
import time
from abc import ABC, abstractmethod
from smtplib import SMTPException, SMTPRecipientsRefused, SMTPSenderRefused

import firebase_admin
from django.conf import settings
from django.core.mail import BadHeaderError, send_mail
from firebase_admin import credentials, messaging
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from .models import Channel

logger = logging.getLogger(__name__)

# Opt-in test knob: artificially slow each send so concurrent workers actually
# overlap (lets you observe work distribution). Default 0 = no delay.
_SEND_DELAY = float(os.environ.get("NOTIFS_SEND_DELAY_SECONDS", "0") or 0)


class PermanentDeliveryError(Exception):
    """Poison: don't retry (invalid token, bad number, hard bounce). -> DLQ."""


class TransientDeliveryError(Exception):
    """Temporary: retry on the backoff ladder (timeout, 5xx, throttling)."""


class NotificationSender(ABC):
    """Common interface every channel sender implements."""

    @abstractmethod
    def send(self, delivery) -> None:
        ...


class EmailSender(NotificationSender):
    """Email sender — currently a STUB (logs only) for testing.

    The real Django-email implementation is preserved below; uncomment it to
    send actual mail via the configured backend (console/SMTP). When enabled, a
    rejected recipient is a permanent failure (-> DLQ) and connection/timeout
    errors are transient (-> retry ladder).
    """

    def send(self, delivery) -> None:
        logger.info("Sending EMAIL to %s | subject=%r", delivery.address, delivery.subject)
        # --- Real email delivery: uncomment to send for real -------------------
        # try:
        #     send_mail(
        #         subject=delivery.subject,
        #         message=delivery.content,
        #         from_email=None,  # falls back to DEFAULT_FROM_EMAIL
        #         recipient_list=[delivery.address],
        #         fail_silently=False,
        #     )
        # except (SMTPRecipientsRefused, SMTPSenderRefused, BadHeaderError) as exc:
        #     raise PermanentDeliveryError(f"email rejected: {exc}") from exc
        # except (SMTPException, OSError) as exc:
        #     raise TransientDeliveryError(f"email send failed: {exc}") from exc
        # -----------------------------------------------------------------------


def _twilio_config():
    """Return (sid, token, from_number) if Twilio is fully configured, else None."""
    sid = getattr(settings, "TWILIO_ACCOUNT_SID", "")
    token = getattr(settings, "TWILIO_AUTH_TOKEN", "")
    from_number = getattr(settings, "TWILIO_PHONE_NUMBER", "")
    if sid and token and from_number:
        return sid, token, from_number
    return None


class SmsSender(NotificationSender):
    """SMS sender — currently a STUB (logs only) for testing.

    The real Twilio implementation is preserved below; uncomment it (and set the
    TWILIO_* settings) to send actual SMS. When enabled, a 5xx/429 from Twilio is
    transient (-> retry ladder); other 4xx errors (invalid/unverified number)
    are permanent (-> DLQ).
    """

    def send(self, delivery) -> None:
        logger.info("Sending SMS to %s", delivery.address)
        # --- Real Twilio delivery: uncomment to send for real ----------------
        # config = _twilio_config()
        # if config is None:
        #     logger.info("Sending SMS to %s (stub: Twilio not configured)", delivery.address)
        #     return
        # sid, token, from_number = config
        # client = Client(sid, token)
        # try:
        #     client.messages.create(
        #         to=delivery.address, from_=from_number, body=delivery.content
        #     )
        #     logger.info("Sent SMS to %s via Twilio", delivery.address)
        # except TwilioRestException as exc:
        #     if exc.status and (exc.status >= 500 or exc.status == 429):
        #         raise TransientDeliveryError(f"twilio {exc.status}: {exc.msg}") from exc
        #     raise PermanentDeliveryError(
        #         f"twilio {exc.status} code={exc.code}: {exc.msg}"
        #     ) from exc
        # except Exception as exc:  # connection/timeout etc.
        #     raise TransientDeliveryError(f"sms send failed: {exc}") from exc
        # ---------------------------------------------------------------------


_firebase_app = None


def _get_firebase_app():
    """Initialise (once) and return the Firebase app, or None if not configured."""
    global _firebase_app
    if _firebase_app is not None:
        return _firebase_app
    path = getattr(settings, "FCM_CREDENTIALS_FILE", "")
    if not path or not os.path.exists(path):
        return None
    cred = credentials.Certificate(path)
    _firebase_app = firebase_admin.initialize_app(cred, name="notser")
    return _firebase_app


class PushSender(NotificationSender):
    """Push sender — currently a STUB (logs only) for testing.

    The real FCM implementation is preserved below; uncomment it (and set
    FCM_CREDENTIALS_FILE) to send actual push. When enabled, an unregistered/
    invalid token (or malformed message) is permanent (-> DLQ); quota/
    availability/network errors are transient (-> retry ladder).
    """

    def send(self, delivery) -> None:
        logger.info("Sending PUSH to %s | title=%r", delivery.address, delivery.subject)
        # --- Real FCM delivery: uncomment to send for real -------------------
        # app = _get_firebase_app()
        # if app is None:
        #     logger.info("Sending PUSH to %s (stub: FCM not configured)", delivery.address)
        #     return
        # message = messaging.Message(
        #     token=delivery.address,
        #     notification=messaging.Notification(
        #         title=delivery.subject or None, body=delivery.content
        #     ),
        # )
        # try:
        #     messaging.send(message, app=app)
        #     logger.info("Sent PUSH to %s via FCM", delivery.address)
        # except (messaging.UnregisteredError, messaging.SenderIdMismatchError) as exc:
        #     raise PermanentDeliveryError(f"fcm token rejected: {exc}") from exc
        # except ValueError as exc:  # malformed token/message
        #     raise PermanentDeliveryError(f"fcm bad message: {exc}") from exc
        # except Exception as exc:  # quota / unavailable / network
        #     raise TransientDeliveryError(f"push send failed: {exc}") from exc
        # ---------------------------------------------------------------------


class InAppSender(NotificationSender):
    def send(self, delivery) -> None:
        logger.info("Sending IN_APP to recipient %s", delivery.address)


# Registry: channel -> sender instance (the Java factory's senderMap).
_SENDERS = {
    Channel.EMAIL: EmailSender(),
    Channel.SMS: SmsSender(),
    Channel.PUSH: PushSender(),
    Channel.IN_APP: InAppSender(),
}


def get_sender(channel: str) -> NotificationSender | None:
    """Return the sender for a channel, or None if unsupported."""
    return _SENDERS.get(channel)


class UnsupportedChannelError(Exception):
    pass


def dispatch(delivery) -> None:
    """Send a single delivery via the sender registered for its channel.

    Raises UnsupportedChannelError if no sender is registered.
    """
    sender = get_sender(delivery.channel)
    if sender is None:
        raise UnsupportedChannelError(delivery.channel)
    if _SEND_DELAY:
        time.sleep(_SEND_DELAY)
    sender.send(delivery)
