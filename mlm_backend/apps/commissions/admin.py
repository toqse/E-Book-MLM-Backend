from django.contrib import admin

from .models import CommissionLedger, MilestoneRecord

_USER_SEARCH = (
    "recipient__member_id",
    "recipient__full_name",
    "recipient__phone",
    "recipient__email",
    "source_user__member_id",
    "source_user__phone",
)


@admin.register(CommissionLedger)
class CommissionLedgerAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "recipient",
        "source_user",
        "order",
        "commission_type",
        "amount",
        "tds_deducted",
        "net_amount",
        "status",
        "slot_band_held",
        "created_at",
    )
    list_filter = ("status", "commission_type", "slot_band_held", "created_at")
    search_fields = _USER_SEARCH + ("order__order_number",)
    autocomplete_fields = ("recipient", "source_user", "order")
    readonly_fields = ("created_at",)
    ordering = ("-id",)


@admin.register(MilestoneRecord)
class MilestoneRecordAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "milestone_referrals",
        "bonus_amount",
        "tds_deducted",
        "net_bonus",
        "status",
        "slot_band_held",
        "created_at",
    )
    list_filter = ("status", "slot_band_held", "milestone_referrals", "created_at")
    search_fields = (
        "user__member_id",
        "user__full_name",
        "user__phone",
        "user__email",
    )
    autocomplete_fields = ("user",)
    readonly_fields = ("created_at",)
    ordering = ("-id",)
