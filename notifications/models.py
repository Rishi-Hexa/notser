import uuid

from django.contrib.postgres.fields import ArrayField
from django.db import models


class Channel(models.TextChoices):
    """The transport a notification goes out on."""
    EMAIL = "EMAIL", "Email"
    SMS = "SMS", "SMS"
    PUSH = "PUSH", "Push"
    IN_APP = "IN_APP", "In-app"


class Priority(models.TextChoices):
    LOW = "LOW", "Low"
    MEDIUM = "MEDIUM", "Medium"
    HIGH = "HIGH", "High"


class Category(models.TextChoices):
    """Drives fail-open/fail-closed behaviour later (see design doc)."""
    TRANSACTIONAL = "TRANSACTIONAL", "Transactional"
    INFORMATIONAL = "INFORMATIONAL", "Informational"
    PROMOTIONAL = "PROMOTIONAL", "Promotional"


class DeliveryStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"            # queued, awaiting a worker
    SENT = "SENT", "Sent"
    RETRYING = "RETRYING", "Retrying"          # transient failure, will retry
    THROTTLED = "THROTTLED", "Throttled"        # rate-limited, deferred
    DEAD_LETTER = "DEAD_LETTER", "Dead-letter"  # permanent failure / exhausted


class NotificationStatus(models.TextChoices):
    SCHEDULED = "SCHEDULED", "Scheduled"  # queued for a future time
    PENDING = "PENDING", "Pending"
    SENT = "SENT", "Sent"          # every delivery sent
    PARTIAL = "PARTIAL", "Partial"  # some sent, some failed
    FAILED = "FAILED", "Failed"     # every delivery failed


class Recipient(models.Model):
    """Someone we can notify. The address used depends on the channel:
    email -> email, sms -> phone, push -> device_token, in-app -> recipient id.
    """

    name = models.CharField(max_length=255, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    device_token = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name or self.email or self.phone or f"recipient#{self.pk}"

    def address_for(self, channel: str) -> str:
        """Return the channel-specific address, or '' if none on file."""
        return {
            Channel.EMAIL: self.email,
            Channel.SMS: self.phone,
            Channel.PUSH: self.device_token,
            Channel.IN_APP: str(self.pk) if self.pk else "",
        }.get(channel, "")


class MissingTemplateVariable(Exception):
    """Raised when a template placeholder has no matching value in `data`."""

    def __init__(self, key):
        self.key = key
        super().__init__(f"Missing template variable: {key!r}")


class _StrictDict(dict):
    """A dict that raises MissingTemplateVariable instead of KeyError, so a
    template referencing an unknown variable fails loudly with a clear message."""

    def __missing__(self, key):
        raise MissingTemplateVariable(key)


class Template(models.Model):
    """A reusable, versioned message template for one channel + locale (F16).

    `subject` and `body` are template strings with ``{placeholder}`` variables
    filled from a caller-supplied data dict at send time.
    """

    name = models.CharField(max_length=100)  # e.g. "order_filled"
    channel = models.CharField(max_length=10, choices=Channel.choices)
    locale = models.CharField(max_length=10, default="en-US")
    version = models.PositiveIntegerField(default=1)

    subject = models.CharField(max_length=255, blank=True)
    body = models.TextField()

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("name", "channel", "locale", "version")
        ordering = ["name", "channel", "locale", "-version"]

    def __str__(self):
        return f"{self.name} [{self.channel}/{self.locale}] v{self.version}"

    def render(self, data: dict) -> tuple[str, str]:
        """Return (subject, body) with ``{placeholders}`` filled from `data`.

        Raises MissingTemplateVariable if a referenced variable is absent.
        """
        safe = _StrictDict(data or {})
        return self.subject.format_map(safe), self.body.format_map(safe)


class Notification(models.Model):
    """The intent: send *this* to *these* recipients on *these* channels.

    Content is supplied either by naming a `template` (+ `data`), in which case
    the per-channel template is rendered for each delivery, or directly via
    `subject`/`content`. The actual fan-out lives in the Delivery rows.
    """

    # notification_id doubles as the trace_id (Slack model in the design doc).
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    priority = models.CharField(
        max_length=10, choices=Priority.choices, default=Priority.MEDIUM
    )
    category = models.CharField(
        max_length=20, choices=Category.choices, default=Category.INFORMATIONAL
    )

    channels = ArrayField(
        models.CharField(max_length=10, choices=Channel.choices), default=list
    )
    recipients = models.ManyToManyField(Recipient, related_name="notifications")

    # Template mode: name + locale + data (per-channel template resolved at send).
    template_name = models.CharField(max_length=100, blank=True)
    locale = models.CharField(max_length=10, default="en-US")
    data = models.JSONField(default=dict, blank=True)

    # Direct mode: used when no template_name is given.
    subject = models.CharField(max_length=255, blank=True)
    content = models.TextField(blank=True)

    # When set, deliver at/after this time instead of immediately (F6/F7).
    scheduled_for = models.DateTimeField(null=True, blank=True)

    status = models.CharField(
        max_length=10, choices=NotificationStatus.choices,
        default=NotificationStatus.PENDING,
    )

    idempotency_key = models.CharField(
        max_length=255, null=True, blank=True, unique=True
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.id} {self.channels} ({self.status})"


class Delivery(models.Model):
    """One attempt to deliver a notification to one recipient on one channel.

    This is the delivery log: per-recipient, per-channel status + failure
    tracking (F26/F27), and the unit a retry will later act on.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    notification = models.ForeignKey(
        Notification, related_name="deliveries", on_delete=models.CASCADE
    )
    recipient = models.ForeignKey(
        Recipient, related_name="deliveries", on_delete=models.CASCADE
    )
    channel = models.CharField(max_length=10, choices=Channel.choices)

    # The specific template row rendered into this delivery, if any.
    template = models.ForeignKey(
        Template, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="deliveries",
    )

    address = models.CharField(max_length=255, blank=True)  # resolved per channel
    subject = models.CharField(max_length=255, blank=True)  # rendered snapshot
    content = models.TextField(blank=True)                  # rendered snapshot

    # Denormalized from the notification so the worker's claim query can order
    # by priority without a join.
    priority = models.CharField(
        max_length=10, choices=Priority.choices, default=Priority.MEDIUM
    )

    status = models.CharField(
        max_length=12, choices=DeliveryStatus.choices, default=DeliveryStatus.PENDING
    )
    attempts = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    # When this delivery next becomes eligible to send (now for PENDING, a
    # future time for RETRYING, null once terminal).
    next_attempt_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # One delivery per recipient+channel within a notification.
        unique_together = ("notification", "recipient", "channel")
        ordering = ["-created_at"]
        indexes = [
            # The worker's hot query: due deliveries by status + time.
            models.Index(fields=["status", "next_attempt_at"]),
        ]

    def __str__(self):
        return f"{self.channel} -> {self.address} ({self.status})"
