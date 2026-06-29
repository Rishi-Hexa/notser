from rest_framework.routers import DefaultRouter

from .views import (
    DeliveryViewSet,
    NotificationViewSet,
    RecipientViewSet,
    TemplateViewSet,
)

router = DefaultRouter()
router.register(r"notifications", NotificationViewSet, basename="notification")
router.register(r"templates", TemplateViewSet, basename="template")
router.register(r"recipients", RecipientViewSet, basename="recipient")
router.register(r"deliveries", DeliveryViewSet, basename="delivery")

urlpatterns = router.urls
