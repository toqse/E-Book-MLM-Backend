"""Shared withdrawal approve / reject / mark-paid used by API and Django admin."""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.wallet.models import Wallet, WalletTransaction, WithdrawalRequest

ZERO = Decimal("0.00")


def _q2(raw: Decimal) -> Decimal:
    return (raw or ZERO).quantize(Decimal("0.01"))


class WithdrawalActionError(Exception):
    def __init__(self, message: str, *, code: str = "invalid_state"):
        self.message = message
        self.code = code
        super().__init__(message)


def approve_withdrawal(wr: WithdrawalRequest, *, actor) -> WithdrawalRequest:
    if wr.status != WithdrawalRequest.Status.PENDING:
        raise WithdrawalActionError(
            "Withdrawal is not pending.",
            code="invalid_state",
        )
    wr.status = WithdrawalRequest.Status.APPROVED
    wr.approved_at = timezone.now()
    wr.approved_by = actor
    wr.save(update_fields=["status", "approved_at", "approved_by", "updated_at"])
    from apps.notifications.lifecycle import schedule_msg91_lifecycle
    from apps.notifications.tasks import send_withdrawal_approved_task

    schedule_msg91_lifecycle(send_withdrawal_approved_task, wr.pk)
    return wr


def reject_withdrawal(wr: WithdrawalRequest, *, reason: str = "") -> WithdrawalRequest:
    if wr.status not in (
        WithdrawalRequest.Status.PENDING,
        WithdrawalRequest.Status.APPROVED,
    ):
        raise WithdrawalActionError(
            "Withdrawal cannot be rejected in this state.",
            code="invalid_state",
        )
    reason = (reason or "").strip()
    with transaction.atomic():
        wallet, _ = Wallet.objects.select_for_update().get_or_create(user=wr.user)
        wallet.cash_balance = _q2(wallet.cash_balance + wr.amount_requested)
        wallet.total_withdrawn = _q2(wallet.total_withdrawn - wr.net_payable)
        wallet.total_tds_deducted = _q2(wallet.total_tds_deducted - wr.tds_amount)
        wallet.band_cash_withdrawn_fy = _q2(
            wallet.band_cash_withdrawn_fy - wr.amount_requested
        )
        wallet.save(
            update_fields=[
                "cash_balance",
                "total_withdrawn",
                "total_tds_deducted",
                "band_cash_withdrawn_fy",
                "updated_at",
            ]
        )
        WalletTransaction.objects.create(
            user=wr.user,
            tx_type=WalletTransaction.TxType.ADJUSTMENT,
            amount=wr.amount_requested,
            balance_after=wallet.cash_balance,
            reference=f"withdrawal_reject:{wr.id}",
            meta={
                "withdrawal_id": wr.id,
                "band": wr.band,
                "tds_amount_reversed": str(wr.tds_amount),
                "net_reversed": str(wr.net_payable),
            },
        )
        wr.status = WithdrawalRequest.Status.REJECTED
        wr.reject_reason = reason
        wr.save(update_fields=["status", "reject_reason", "updated_at"])
        from apps.notifications.lifecycle import schedule_msg91_lifecycle
        from apps.notifications.tasks import send_withdrawal_rejected_task

        schedule_msg91_lifecycle(send_withdrawal_rejected_task, wr.pk, reason)
    return wr


def mark_withdrawal_paid(
    wr: WithdrawalRequest,
    *,
    actor,
    utr_number: str,
    paid_at=None,
) -> WithdrawalRequest:
    utr = (utr_number or "").strip()
    if not utr:
        raise WithdrawalActionError("utr_number is required.", code="missing_utr")
    if wr.status not in (
        WithdrawalRequest.Status.APPROVED,
        WithdrawalRequest.Status.PROCESSING,
    ):
        raise WithdrawalActionError(
            "Withdrawal cannot be marked paid in this state.",
            code="invalid_state",
        )
    wr.status = WithdrawalRequest.Status.PAID
    wr.utr_number = utr
    wr.paid_at = paid_at or timezone.now()
    wr.paid_by = actor
    wr.save(update_fields=["status", "utr_number", "paid_at", "paid_by", "updated_at"])
    return wr
