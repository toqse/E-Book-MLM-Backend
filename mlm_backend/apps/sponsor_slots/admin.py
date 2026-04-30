from django.contrib import admin

from .models import SponsorSlotBatch, SponsorSlotCode


@admin.register(SponsorSlotBatch)
class SponsorSlotBatchAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "issued_to",
        "band_number",
        "total_codes",
        "codes_redeemed",
        "codes_expired",
        "expires_at",
        "created_at",
    )
    list_filter = ("band_number", "expires_at", "created_at")
    search_fields = ("issued_to__member_id", "issued_to__phone")
    autocomplete_fields = ("issued_to",)
    readonly_fields = ("created_at",)
    ordering = ("-id",)


@admin.register(SponsorSlotCode)
class SponsorSlotCodeAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "code",
        "issued_to",
        "status",
        "shared_via",
        "redeemed_by",
        "expires_at",
        "is_flagged",
    )
    list_filter = ("status", "shared_via", "is_flagged", "expires_at", "created_at")
    search_fields = (
        "code",
        "issued_to__member_id",
        "issued_to__phone",
        "redeemed_by__member_id",
        "redeemed_by__phone",
    )
    autocomplete_fields = ("batch", "issued_to", "redeemed_by", "redeemed_order")
    readonly_fields = ("created_at",)
    ordering = ("-id",)
