from django.utils import timezone
from rest_framework import serializers

from .models import (
    Category,
    Channel,
    Delivery,
    Notification,
    Priority,
    Recipient,
    Template,
)


class RecipientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Recipient
        fields = ["id", "name", "email", "phone", "device_token", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]


class TemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Template
        fields = [
            "id", "name", "channel", "locale", "version",
            "subject", "body", "is_active", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class DeliverySerializer(serializers.ModelSerializer):
    """Read-only view of one fan-out delivery."""

    class Meta:
        model = Delivery
        fields = [
            "id", "notification", "recipient", "channel", "priority", "address",
            "subject", "content", "status", "attempts", "error",
            "sent_at", "next_attempt_at", "created_at",
        ]


class NotificationSerializer(serializers.ModelSerializer):
    """Read view of a notification, including its fan-out deliveries."""

    recipients = serializers.PrimaryKeyRelatedField(many=True, read_only=True)
    deliveries = DeliverySerializer(many=True, read_only=True)

    class Meta:
        model = Notification
        fields = [
            "id", "priority", "category", "channels", "recipients",
            "template_name", "locale", "data", "subject", "content",
            "scheduled_for", "status", "idempotency_key",
            "created_at", "updated_at", "deliveries",
        ]


class NotificationCreateSerializer(serializers.Serializer):
    """Validates a send request. Content comes from either `template` + `data`
    or directly via `subject`/`content`."""

    recipients = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Recipient.objects.all(), allow_empty=False
    )
    channels = serializers.ListField(
        child=serializers.ChoiceField(choices=Channel.choices), allow_empty=False
    )
    priority = serializers.ChoiceField(
        choices=Priority.choices, required=False, default=Priority.MEDIUM
    )
    category = serializers.ChoiceField(
        choices=Category.choices, required=False, default=Category.INFORMATIONAL
    )
    template = serializers.CharField(required=False, allow_blank=True, default="")
    locale = serializers.CharField(required=False, default="en-US")
    data = serializers.JSONField(required=False, default=dict)
    subject = serializers.CharField(required=False, allow_blank=True, default="")
    content = serializers.CharField(required=False, allow_blank=True, default="")
    idempotency_key = serializers.CharField(required=False, allow_blank=True)

    # Scheduling (F6/F7): deliver at this future time instead of immediately.
    send_at = serializers.DateTimeField(required=False)

    def validate(self, attrs):
        if not attrs.get("template") and not attrs.get("content"):
            raise serializers.ValidationError(
                "Provide either 'template' (+ data) or 'content'."
            )
        # De-duplicate channels while preserving order.
        attrs["channels"] = list(dict.fromkeys(attrs["channels"]))

        send_at = attrs.get("send_at")
        if send_at is not None and send_at <= timezone.now():
            raise serializers.ValidationError({"send_at": "must be in the future."})
        attrs["scheduled_for"] = send_at
        return attrs
