"""Fan-out, async delivery, retry, and dead-lettering.

Two halves:
  * enqueue_notification() — turns a Notification into PENDING Delivery rows
    (resolves address + per-channel content). No sending happens here.
  * the worker side — process_due_deliveries() claims due deliveries, sends
    them, and on failure either schedules a retry on the backoff ladder or
    dead-letters them.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Case, Count, IntegerField, When
from django.utils import timezone

from . import senders
from .models import (
    Channel,
    Delivery,
    DeliveryStatus,
    MissingTemplateVariable,
    Notification,
    NotificationStatus,
    Priority,
    Template,
)

# Lower number = processed first. Used to order the worker's claim query.
_PRIORITY_RANK = Case(
    When(priority=Priority.HIGH, then=0),
    When(priority=Priority.MEDIUM, then=1),
    When(priority=Priority.LOW, then=2),
    default=1,
    output_field=IntegerField(),
)


def _rate_limit_for(channel):
    """(max_sends, window_seconds) for a channel, or None if unlimited."""
    return getattr(settings, "NOTIFS_RATE_LIMITS", {}).get(channel)


def _within_rate_limit(delivery):
    """Check the per-recipient, per-channel rate limit using a sliding window of
    actual SENT deliveries.

    Returns (allowed, retry_at): if over the limit, retry_at is when the oldest
    in-window send ages out and a slot frees.
    """
    conf = _rate_limit_for(delivery.channel)
    if not conf:
        return True, None
    limit, window = conf
    window_start = timezone.now() - timedelta(seconds=window)
    sent_times = list(
        Delivery.objects.filter(
            recipient_id=delivery.recipient_id,
            channel=delivery.channel,
            status=DeliveryStatus.SENT,
            sent_at__gte=window_start,
        )
        .order_by("sent_at")
        .values_list("sent_at", flat=True)
    )
    if len(sent_times) < limit:
        return True, None
    # A slot frees when the (count - limit)-th oldest send leaves the window.
    free_at = sent_times[len(sent_times) - limit] + timedelta(seconds=window)
    return False, max(free_at, timezone.now() + timedelta(seconds=1))

logger = logging.getLogger(__name__)

# Tiered retry backoff: wait 5s before retry #1, 1m before #2, 1h before #3.
# After this many failed attempts, the delivery is dead-lettered.
RETRY_DELAYS = [5, 30, 60]


class NotificationConfigError(Exception):
    """A caller/config error that should abort the whole send (e.g. a missing
    template or an unfilled template variable) — as opposed to a per-delivery
    data problem like a recipient missing an address."""


# --------------------------------------------------------------------------- #
# Content resolution
# --------------------------------------------------------------------------- #
def resolve_template(name, channel, locale):
    """Latest active template row for a name+channel+locale, or None."""
    return (
        Template.objects.filter(
            name=name, channel=channel, locale=locale, is_active=True
        )
        .order_by("-version")
        .first()
    )


def content_for_channel(notification, channel):
    """Return (subject, content, template_row) for a channel.

    Raises NotificationConfigError if a named template is missing for the
    channel or a referenced variable has no value.
    """
    if notification.template_name:
        template = resolve_template(
            notification.template_name, channel, notification.locale
        )
        if template is None:
            raise NotificationConfigError(
                f"No active {channel} template '{notification.template_name}' "
                f"for locale '{notification.locale}'"
            )
        try:
            subject, content = template.render(notification.data)
        except MissingTemplateVariable as exc:
            raise NotificationConfigError(str(exc))
        return subject, content, template
    return notification.subject, notification.content, None


# --------------------------------------------------------------------------- #
# Enqueue (producer side) — fan out, no sending
# --------------------------------------------------------------------------- #
def enqueue_notification(notification):
    """Fan out into PENDING Delivery rows. Returns the created rows.

    Raises NotificationConfigError (before creating anything) if content can't
    be resolved for a channel. A recipient missing an address for a channel is
    a permanent per-delivery failure and is dead-lettered immediately.
    """
    recipients = list(notification.recipients.all())

    # Resolve content per channel up front so a config error aborts cleanly.
    channel_content = {
        channel: content_for_channel(notification, channel)
        for channel in notification.channels
    }

    # Scheduled notifications become eligible at their scheduled time; others now.
    eligible_at = notification.scheduled_for or timezone.now()
    deliveries = []
    for channel in notification.channels:
        subject, content, template = channel_content[channel]
        for recipient in recipients:
            address = recipient.address_for(channel)
            delivery = Delivery(
                notification=notification,
                recipient=recipient,
                channel=channel,
                template=template,
                address=address,
                subject=subject or "",
                content=content or "",
                priority=notification.priority,
            )
            if not address:
                delivery.status = DeliveryStatus.DEAD_LETTER
                delivery.error = f"recipient has no {channel} address"
            else:
                delivery.status = DeliveryStatus.PENDING
                delivery.next_attempt_at = eligible_at
            deliveries.append(delivery)

    Delivery.objects.bulk_create(deliveries)
    recompute_notification_status(notification.id)

    dead_on_enqueue = sum(
        1 for d in deliveries if d.status == DeliveryStatus.DEAD_LETTER
    )
    logger.info(
        "notif=%s enqueued %d deliveries (channels=%s, recipients=%d)",
        notification.id, len(deliveries), list(notification.channels), len(recipients),
    )
    if dead_on_enqueue:
        logger.warning(
            "notif=%s %d deliveries dead-lettered at enqueue (missing address)",
            notification.id, dead_on_enqueue,
        )
    return deliveries


# --------------------------------------------------------------------------- #
# Worker (consumer side) — send, retry, dead-letter
# --------------------------------------------------------------------------- #
def process_due_deliveries(batch=50):
    """Claim and process up to `batch` deliveries that are due now.

    Returns a list of (delivery_id, priority, status) for the ones this call
    actually handled. Safe to run from multiple workers: each delivery is
    row-locked with skip_locked so two workers never grab the same one.
    """
    now = timezone.now()
    due_ids = list(
        Delivery.objects.filter(
            status__in=[
                DeliveryStatus.PENDING,
                DeliveryStatus.RETRYING,
                DeliveryStatus.THROTTLED,
            ],
            next_attempt_at__lte=now,
        )
        # HIGH before MEDIUM before LOW, then oldest-due first within a priority.
        .annotate(prank=_PRIORITY_RANK)
        .order_by("prank", "next_attempt_at")
        .values_list("id", flat=True)[:batch]
    )

    processed = []
    for delivery_id in due_ids:
        outcome = _process_one(delivery_id)
        if outcome is not None:
            processed.append(outcome)
    return processed


def _process_one(delivery_id):
    """Lock, re-check, send, and update one delivery.

    Returns (delivery_id, priority, status) if this worker handled it, else None
    (another worker had it, or it was no longer due).

    Note: for the stub senders the network call is instant, so holding the row
    lock across the send is fine. With real providers, switch to a claim-then-
    send pattern so a slow provider call doesn't hold the lock.
    """
    with transaction.atomic():
        delivery = (
            Delivery.objects.select_for_update(skip_locked=True)
            .filter(id=delivery_id)
            .first()
        )
        if delivery is None:
            return None  # locked by another worker, or gone
        # Re-check it's still due (another worker may have handled it).
        claimable = (
            DeliveryStatus.PENDING,
            DeliveryStatus.RETRYING,
            DeliveryStatus.THROTTLED,
        )
        if delivery.status not in claimable:
            return None
        if delivery.next_attempt_at and delivery.next_attempt_at > timezone.now():
            return None

        # Rate limit: defer (don't drop) if the recipient+channel is over budget.
        allowed, retry_at = _within_rate_limit(delivery)
        if not allowed:
            limit, window = _rate_limit_for(delivery.channel)
            delivery.status = DeliveryStatus.THROTTLED
            delivery.next_attempt_at = retry_at
            delivery.error = (
                f"rate-limited: max {limit} {delivery.channel} per {window}s "
                f"per recipient; deferred"
            )
            delivery.save(
                update_fields=["status", "next_attempt_at", "error", "updated_at"]
            )
            logger.warning(
                "notif=%s delivery=%s %s -> THROTTLED deferred=%s",
                delivery.notification_id, delivery.id, delivery.channel, retry_at,
            )
        else:
            _attempt(delivery)

    recompute_notification_status(delivery.notification_id)
    return (str(delivery.id), delivery.priority, delivery.status)


def _attempt(delivery):
    """Send one delivery and record the outcome (called with the row locked)."""
    delivery.attempts += 1
    tag = (delivery.notification_id, delivery.id, delivery.channel, delivery.attempts)
    try:
        senders.dispatch(delivery)
    except senders.PermanentDeliveryError as exc:
        delivery.status = DeliveryStatus.DEAD_LETTER
        delivery.error = f"permanent: {exc}"
        delivery.next_attempt_at = None
        logger.warning(
            "notif=%s delivery=%s %s -> DEAD_LETTER (permanent) attempt=%d: %s",
            *tag, exc,
        )
    except Exception as exc:  # TransientDeliveryError + anything unexpected
        if delivery.attempts <= len(RETRY_DELAYS):
            delay = RETRY_DELAYS[delivery.attempts - 1]
            delivery.status = DeliveryStatus.RETRYING
            delivery.next_attempt_at = timezone.now() + timedelta(seconds=delay)
            delivery.error = f"transient (attempt {delivery.attempts}): {exc}"
            logger.warning(
                "notif=%s delivery=%s %s -> RETRYING attempt=%d next=%s: %s",
                *tag, delivery.next_attempt_at, exc,
            )
        else:
            delivery.status = DeliveryStatus.DEAD_LETTER
            delivery.error = f"exhausted after {delivery.attempts} attempts: {exc}"
            delivery.next_attempt_at = None
            logger.warning(
                "notif=%s delivery=%s %s -> DEAD_LETTER (exhausted) attempt=%d: %s",
                *tag, exc,
            )
    else:
        delivery.status = DeliveryStatus.SENT
        delivery.sent_at = timezone.now()
        delivery.next_attempt_at = None
        delivery.error = ""
        logger.info("notif=%s delivery=%s %s -> SENT attempt=%d", *tag)

    delivery.save(
        update_fields=[
            "attempts", "status", "error", "sent_at", "next_attempt_at", "updated_at"
        ]
    )


def replay_delivery(delivery):
    """Re-queue a dead-lettered delivery after a fix (DLQ replay, F10).

    Resets attempts so it walks the retry ladder fresh. Returns True if it was
    re-queued, False if it wasn't in the dead-letter state.
    """
    if delivery.status != DeliveryStatus.DEAD_LETTER:
        return False
    delivery.status = DeliveryStatus.PENDING
    delivery.attempts = 0
    delivery.error = ""
    delivery.next_attempt_at = timezone.now()
    delivery.save(
        update_fields=["status", "attempts", "error", "next_attempt_at", "updated_at"]
    )
    recompute_notification_status(delivery.notification_id)
    return True


# --------------------------------------------------------------------------- #
# Aggregate status
# --------------------------------------------------------------------------- #
def recompute_notification_status(notification_id):
    statuses = list(
        Delivery.objects.filter(notification_id=notification_id).values_list(
            "status", flat=True
        )
    )
    status = _aggregate_status(statuses)
    # If it's still pending but parked for a future time, surface that as SCHEDULED.
    if status == NotificationStatus.PENDING:
        scheduled_for = (
            Notification.objects.filter(id=notification_id)
            .values_list("scheduled_for", flat=True)
            .first()
        )
        if scheduled_for and scheduled_for > timezone.now():
            status = NotificationStatus.SCHEDULED
    Notification.objects.filter(id=notification_id).update(
        status=status, updated_at=timezone.now()
    )


def pipeline_stats():
    """Roll up the pipeline's health for the observability endpoint.

    Counts deliveries + notifications by status, queue depth (what's due now vs
    parked for later), the DLQ size, and per-channel success rate.
    """
    now = timezone.now()

    delivery_counts = {s: 0 for s in DeliveryStatus.values}
    for status, n in Delivery.objects.values_list("status").annotate(n=Count("id")):
        delivery_counts[status] = n

    notification_counts = {s: 0 for s in NotificationStatus.values}
    for status, n in Notification.objects.values_list("status").annotate(n=Count("id")):
        notification_counts[status] = n

    active = [DeliveryStatus.PENDING, DeliveryStatus.RETRYING, DeliveryStatus.THROTTLED]
    due_now = Delivery.objects.filter(status__in=active, next_attempt_at__lte=now).count()
    scheduled_future = Delivery.objects.filter(
        status__in=active, next_attempt_at__gt=now
    ).count()

    by_channel = {}
    for channel, status, n in (
        Delivery.objects.values_list("channel", "status").annotate(n=Count("id"))
    ):
        by_channel.setdefault(channel, {})[status] = n

    channels = {}
    for channel in Channel.values:
        counts = by_channel.get(channel, {})
        sent = counts.get(DeliveryStatus.SENT, 0)
        dead = counts.get(DeliveryStatus.DEAD_LETTER, 0)
        terminal = sent + dead
        channels[channel] = {
            "total": sum(counts.values()),
            "sent": sent,
            "dead_letter": dead,
            "success_rate": round(sent / terminal, 4) if terminal else None,
        }

    return {
        "deliveries": delivery_counts,
        "notifications": notification_counts,
        "queue": {
            "due_now": due_now,                  # what the worker would claim now
            "scheduled_future": scheduled_future,  # parked (scheduled/retry/throttled)
            "dlq_size": delivery_counts[DeliveryStatus.DEAD_LETTER],
        },
        "channels": channels,
    }


def _aggregate_status(statuses):
    if not statuses:
        return NotificationStatus.PENDING
    # Anything still in flight => the notification is still pending.
    in_flight = (
        DeliveryStatus.PENDING,
        DeliveryStatus.RETRYING,
        DeliveryStatus.THROTTLED,
    )
    if any(s in in_flight for s in statuses):
        return NotificationStatus.PENDING
    sent = sum(1 for s in statuses if s == DeliveryStatus.SENT)
    if sent == len(statuses):
        return NotificationStatus.SENT
    if sent == 0:
        return NotificationStatus.FAILED
    return NotificationStatus.PARTIAL
