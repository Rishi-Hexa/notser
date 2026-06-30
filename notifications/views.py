import logging

from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import Category, Delivery, Notification, Priority, Recipient, Template
from .serializers import (
    DeliverySerializer,
    NotificationCreateSerializer,
    NotificationSerializer,
    RecipientSerializer,
    TemplateSerializer,
)

logger = logging.getLogger(__name__)


class StatsView(APIView):
    """Pipeline health roll-up (observability).

    GET /api/v1/stats/ -> delivery/notification status counts, queue depth,
    DLQ size, and per-channel success rate.
    """

    def get(self, request):
        return Response(services.pipeline_stats())


class RecipientViewSet(viewsets.ModelViewSet):
    """CRUD for people we can notify. /api/v1/recipients/"""

    queryset = Recipient.objects.all()
    serializer_class = RecipientSerializer


class TemplateViewSet(viewsets.ModelViewSet):
    """CRUD for reusable, versioned message templates (F16). /api/v1/templates/"""

    queryset = Template.objects.all()
    serializer_class = TemplateSerializer


class DeliveryViewSet(
    mixins.RetrieveModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet
):
    """Read-only view of fan-out deliveries, plus DLQ replay (F10/F26/F27).

    GET  /api/v1/deliveries/                 list (filter: ?status=, ?notification=)
    GET  /api/v1/deliveries/{id}/            fetch one
    POST /api/v1/deliveries/{id}/replay/     re-queue a dead-lettered delivery
    """

    serializer_class = DeliverySerializer

    def get_queryset(self):
        qs = Delivery.objects.all()
        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param.upper())
        notification_param = self.request.query_params.get("notification")
        if notification_param:
            qs = qs.filter(notification_id=notification_param)
        return qs

    @action(detail=True, methods=["post"])
    def replay(self, request, pk=None):
        delivery = self.get_object()
        if not services.replay_delivery(delivery):
            raise ValidationError(
                {"detail": f"Only dead-lettered deliveries can be replayed "
                           f"(this one is {delivery.status})."}
            )
        delivery.refresh_from_db()
        return Response(self.get_serializer(delivery).data, status=status.HTTP_200_OK)


class NotificationViewSet(
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    """Accept a notification and queue it for async delivery.

    POST /api/v1/notifications/        accept + fan out to PENDING (202)
    GET  /api/v1/notifications/        list
    GET  /api/v1/notifications/{id}/   fetch one + its deliveries (live status)

    Body: recipients[] + channels[] + (template + data | subject + content).
    Delivery happens in the background worker (manage.py run_worker).
    """

    queryset = Notification.objects.prefetch_related("recipients", "deliveries").all()
    serializer_class = NotificationSerializer

    def create(self, request, *args, **kwargs):
        write = NotificationCreateSerializer(data=request.data)
        write.is_valid(raise_exception=True)
        vd = write.validated_data

        # Idempotency: a re-send with a key we've already seen returns the
        # original notification and does NOT enqueue again.
        key = vd.get("idempotency_key") or None
        if key:
            existing = Notification.objects.filter(idempotency_key=key).first()
            if existing is not None:
                logger.info(
                    "idempotency hit key=%s -> returning existing notif=%s",
                    key, existing.id,
                )
                return Response(
                    NotificationSerializer(existing).data, status=status.HTTP_200_OK
                )

        recipients = vd["recipients"]
        notification = Notification.objects.create(
            priority=vd.get("priority", Priority.MEDIUM),
            category=vd.get("category", Category.INFORMATIONAL),
            channels=vd["channels"],
            template_name=vd.get("template", ""),
            locale=vd.get("locale", "en-US"),
            data=vd.get("data", {}),
            subject=vd.get("subject", ""),
            content=vd.get("content", ""),
            scheduled_for=vd.get("scheduled_for"),
            idempotency_key=key,
        )
        notification.recipients.set(recipients)
        logger.info(
            "notif=%s accepted channels=%s recipients=%d priority=%s",
            notification.id, vd["channels"], len(recipients), notification.priority,
        )

        try:
            services.enqueue_notification(notification)
        except services.NotificationConfigError as exc:
            # Nothing was queued — drop the notification and report the problem.
            logger.error("notif=%s rejected: %s", notification.id, exc)
            notification.delete()
            raise ValidationError({"detail": str(exc)})

        notification.refresh_from_db()
        # 202: accepted and queued; the worker delivers asynchronously.
        return Response(
            NotificationSerializer(notification).data, status=status.HTTP_202_ACCEPTED
        )
