from django.contrib import admin

from .models import TdsLedger


@admin.register(TdsLedger)
class TdsLedgerAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "financial_year",
        "section",
        "total_earned",
        "total_tds",
        "tds_triggered",
        "tds_triggered_at",
        "updated_at",
    )
    list_filter = ("financial_year", "section", "tds_triggered")
    search_fields = (
        "user__member_id",
        "user__full_name",
        "user__phone",
        "user__email",
    )
    autocomplete_fields = ("user",)
    readonly_fields = ("created_at", "updated_at")
    ordering = ("-updated_at",)
