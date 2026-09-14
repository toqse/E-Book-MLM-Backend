from django.contrib import admin, messages

from apps.wallet.services.withdrawal_admin import (
    WithdrawalActionError,
    approve_withdrawal,
    mark_withdrawal_paid,
    reject_withdrawal,
)

from .models import Wallet, WalletTransaction, WithdrawalRequest

_USER_SEARCH = (
    "user__member_id",
    "user__full_name",
    "user__phone",
    "user__email",
)


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "cash_balance",
        "total_earned",
        "total_withdrawn",
        "total_tds_deducted",
        "tds_payable",
        "current_band",
        "fy_label",
        "updated_at",
    )
    list_filter = ("current_band", "fy_label")
    search_fields = _USER_SEARCH
    autocomplete_fields = ("user",)
    readonly_fields = ("updated_at",)
    ordering = ("-updated_at",)


@admin.register(WalletTransaction)
class WalletTransactionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "tx_type",
        "amount",
        "balance_after",
        "reference",
        "created_at",
    )
    list_filter = ("tx_type", "created_at")
    search_fields = _USER_SEARCH + ("reference",)
    autocomplete_fields = ("user",)
    readonly_fields = ("created_at",)
    ordering = ("-created_at",)


@admin.action(description="Approve selected pending withdrawals")
def approve_selected_withdrawals(modeladmin, request, queryset):
    ok = 0
    for wr in queryset.select_related("user"):
        try:
            approve_withdrawal(wr, actor=request.user)
            ok += 1
        except WithdrawalActionError as exc:
            modeladmin.message_user(
                request,
                f"#{wr.pk}: {exc.message}",
                level=messages.ERROR,
            )
    if ok:
        modeladmin.message_user(request, f"Approved {ok} withdrawal(s).", messages.SUCCESS)


@admin.action(description="Reject selected withdrawals (refund wallet)")
def reject_selected_withdrawals(modeladmin, request, queryset):
    ok = 0
    for wr in queryset.select_related("user"):
        try:
            reject_withdrawal(wr, reason="Rejected via Django admin")
            ok += 1
        except WithdrawalActionError as exc:
            modeladmin.message_user(
                request,
                f"#{wr.pk}: {exc.message}",
                level=messages.ERROR,
            )
    if ok:
        modeladmin.message_user(request, f"Rejected {ok} withdrawal(s).", messages.SUCCESS)


@admin.action(description="Mark selected as paid (requires utr_number set)")
def mark_selected_withdrawals_paid(modeladmin, request, queryset):
    ok = 0
    for wr in queryset.select_related("user"):
        try:
            mark_withdrawal_paid(
                wr,
                actor=request.user,
                utr_number=wr.utr_number or "",
            )
            ok += 1
        except WithdrawalActionError as exc:
            modeladmin.message_user(
                request,
                f"#{wr.pk}: {exc.message}",
                level=messages.ERROR,
            )
    if ok:
        modeladmin.message_user(
            request, f"Marked {ok} withdrawal(s) as paid.", messages.SUCCESS
        )


@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "status",
        "band",
        "amount_requested",
        "tds_amount",
        "net_payable",
        "payout_method",
        "utr_number",
        "created_at",
        "paid_at",
    )
    list_filter = ("status", "payout_method", "band", "created_at")
    search_fields = _USER_SEARCH + ("utr_number", "razorpay_payout_id")
    autocomplete_fields = ("user", "approved_by", "paid_by")
    readonly_fields = ("created_at", "updated_at")
    ordering = ("-id",)
    actions = (
        approve_selected_withdrawals,
        reject_selected_withdrawals,
        mark_selected_withdrawals_paid,
    )
    fieldsets = (
        (None, {"fields": ("user", "status", "band", "payout_method", "payout_destination_hint")}),
        (
            "Amounts",
            {"fields": ("amount_requested", "tds_amount", "net_payable", "tds_section")},
        ),
        (
            "Payout",
            {
                "fields": (
                    "razorpay_payout_id",
                    "utr_number",
                    "approved_at",
                    "approved_by",
                    "paid_at",
                    "paid_by",
                    "reject_reason",
                )
            },
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )
