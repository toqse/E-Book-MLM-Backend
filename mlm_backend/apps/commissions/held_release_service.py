"""Admin-triggered release of HELD / backlog commission ledger rows (no hook on KYC approve)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.db import transaction

from apps.admin_panel.utils import get_system_config
from apps.audit.services import write_audit
from apps.payments.models import Order
from apps.users.models import User
from apps.wallet.bands import on_total_earned_updated
from apps.wallet.models import Wallet, WalletTransaction
from apps.tds.services import calculate_and_apply_194h_tds

from .models import CommissionLedger


def _skip(entry_id: int, reason: str) -> dict[str, Any]:
    return {"id": entry_id, "reason": reason}


@transaction.atomic
def release_held_commissions_for_user(
    *,
    user_id: int,
    actor: User | None = None,
) -> dict[str, Any]:
    """
    Credit wallet for HELD (and eligible PENDING) book-commission rows for recipient user_id.

    Does not run on KYC approval; finance admin calls this explicitly.
    Mirrors CommissionEngine._credit_user payout rules; updates rows in place; may split remainder HELD.
    """
    cfg = get_system_config()
    cap = cfg.earning_cap

    if not User.objects.filter(pk=user_id).exists():
        return {
            "ok": False,
            "credited_ids": [],
            "skipped": [],
            "detail": "user_not_found",
        }

    qs = (
        CommissionLedger.objects.filter(recipient_id=user_id)
        .exclude(commission_type=CommissionLedger.CommissionType.MILESTONE)
        .filter(
            status__in=(CommissionLedger.Status.HELD, CommissionLedger.Status.PENDING),
            net_amount=Decimal("0"),
        )
        .select_related("recipient", "source_user", "order")
        .order_by("id")
    )

    credited_ids: list[int] = []
    skipped: list[dict[str, Any]] = []

    for entry in qs:
        entry = CommissionLedger.objects.select_for_update().select_related(
            "recipient", "source_user", "order"
        ).get(pk=entry.pk)
        if entry.status not in (
            CommissionLedger.Status.HELD,
            CommissionLedger.Status.PENDING,
        ):
            continue

        recipient = User.objects.select_for_update().get(pk=entry.recipient_id)
        order = entry.order

        if recipient.kyc_status != User.KYCStatus.VERIFIED:
            skipped.append(_skip(entry.id, "kyc_not_verified"))
            continue
        if order.status != Order.Status.PAID:
            skipped.append(_skip(entry.id, "order_not_paid"))
            continue

        wallet, _ = Wallet.objects.select_for_update().get_or_create(user=recipient)
        remaining = cap - wallet.total_earned
        if remaining <= 0:
            skipped.append(_skip(entry.id, "earning_cap_reached"))
            continue

        orig_gross = entry.amount
        gross_credit = min(orig_gross, remaining)
        if gross_credit <= 0:
            skipped.append(_skip(entry.id, "zero_gross_credit"))
            continue

        tds = calculate_and_apply_194h_tds(user=recipient, gross_amount=gross_credit)
        wallet.cash_balance += tds.net_amount
        wallet.total_earned += tds.net_amount
        wallet.total_tds_deducted += tds.tds_amount
        wallet.save()

        WalletTransaction.objects.create(
            user=recipient,
            tx_type=WalletTransaction.TxType.CREDIT,
            amount=tds.net_amount,
            balance_after=wallet.cash_balance,
            reference=f"COMM-{order.order_number}",
            meta={
                "type": entry.commission_type,
                "gross": str(tds.gross_amount),
                "tds": str(tds.tds_amount),
                "tds_rate_percent": str(tds.tds_rate_percent),
                "financial_year": tds.financial_year,
                "admin_held_release": True,
            },
        )

        entry.amount = tds.gross_amount
        entry.tds_deducted = tds.tds_amount
        entry.net_amount = tds.net_amount
        entry.status = CommissionLedger.Status.CREDITED
        entry.save(update_fields=["amount", "tds_deducted", "net_amount", "status"])

        remainder = orig_gross - gross_credit
        if remainder > 0:
            CommissionLedger.objects.create(
                recipient=recipient,
                source_user=entry.source_user,
                order=order,
                commission_type=entry.commission_type,
                amount=remainder,
                tds_deducted=Decimal("0"),
                net_amount=Decimal("0"),
                status=CommissionLedger.Status.HELD,
            )

        if wallet.total_earned >= cap:
            recipient.account_status = User.AccountStatus.CAPPED
            recipient.save(update_fields=["account_status"])

        on_total_earned_updated(wallet)
        credited_ids.append(entry.id)

    write_audit(
        "commission.admin_held_release",
        actor=actor,
        target_type="User",
        target_id=str(user_id),
        payload={
            "credited_ids": credited_ids,
            "skipped": skipped,
        },
    )

    return {
        "ok": True,
        "credited_ids": credited_ids,
        "skipped": skipped,
        "processed_count": len(credited_ids),
    }
