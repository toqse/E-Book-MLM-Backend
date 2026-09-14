from django.contrib import admin

from .models import NotificationLog


@admin.register(NotificationLog)
class NotificationLogAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "channel",
        "template_key",
        "sent_at",
    )
    list_filter = ("channel", "template_key", "sent_at")
    search_fields = (
        "template_key",
        "user__member_id",
        "user__full_name",
        "user__phone",
        "user__email",
    )
    autocomplete_fields = ("user",)
    readonly_fields = ("user", "channel", "template_key", "payload", "sent_at")
    ordering = ("-sent_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
