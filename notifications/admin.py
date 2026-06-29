from django.contrib import admin

from .models import Delivery, Notification, Recipient, Template


@admin.register(Recipient)
class RecipientAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "email", "phone", "created_at")
    search_fields = ("name", "email", "phone")


class DeliveryInline(admin.TabularInline):
    model = Delivery
    extra = 0
    readonly_fields = ("id", "recipient", "channel", "address", "status", "error", "sent_at")
    can_delete = False


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("id", "channels", "priority", "category", "status", "scheduled_for", "created_at")
    list_filter = ("priority", "category", "status")
    search_fields = ("idempotency_key", "template_name")
    readonly_fields = ("id", "created_at", "updated_at")
    inlines = [DeliveryInline]


@admin.register(Delivery)
class DeliveryAdmin(admin.ModelAdmin):
    list_display = (
        "id", "channel", "priority", "address", "status", "attempts",
        "next_attempt_at", "sent_at",
    )
    list_filter = ("channel", "priority", "status")
    search_fields = ("address",)
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(Template)
class TemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "channel", "locale", "version", "is_active", "updated_at")
    list_filter = ("channel", "locale", "is_active")
    search_fields = ("name", "subject", "body")
    readonly_fields = ("created_at", "updated_at")
