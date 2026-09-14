from django.contrib import admin

from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "action",
        "actor",
        "target_type",
        "target_id",
        "ip_address",
        "created_at",
    )
    list_filter = ("action", "target_type", "created_at")
    search_fields = (
        "action",
        "target_type",
        "target_id",
        "actor__member_id",
        "actor__email",
        "actor__phone",
    )
    autocomplete_fields = ("actor",)
    readonly_fields = (
        "actor",
        "action",
        "target_type",
        "target_id",
        "payload",
        "ip_address",
        "created_at",
    )
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
