from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import transaction
from django.db.models import Sum
from django.utils.dateparse import parse_date
from django.utils import timezone
from datetime import timedelta

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request

from apps.admin_panel.utils import get_system_config
from apps.common.permissions import IsFinanceAdmin
from apps.common.responses import envelope_response
from apps.users.models import User
from apps.tds.services import calculate_and_apply_194h_tds
from apps.wallet.services.member_money import (
    build_band_ladder,
    build_payouts_bundle,
    get_wallet_row,
)

from .bands import describe_bands_status
from .models import Wallet, WalletTransaction, WithdrawalRequest


CASH_BANDS = frozenset({1, 3, 5, 7, 9})
SLOT_BANDS = frozenset({2, 4, 6, 8})
MIN_WITHDRAWAL = Decimal("200.00")
ZERO = Decimal("0.00")
COOLING_DAYS = 7


def _q2(raw: Decimal) -> Decimal:
    return (raw or ZERO).quantize(Decimal("0.01"))


def _parse_amount(raw: Any) -> Decimal | None:
    try:
        return _q2(Decimal(str(raw)))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _mask_last4(s: str) -> str:
    s = (s or "").strip()
    return s[-4:] if len(s) >= 4 else s


def _mask_bank_account(acct: str) -> str:
    acct = (acct or "").strip()
    if not acct:
        return ""
    last4 = _mask_last4(acct)
    return f"XXXX{last4}" if last4 else "XXXX"


def _mask_upi(upi: str) -> str:
    upi = (upi or "").strip()
    if not upi:
        return ""
    if "@" not in upi:
        return "****"
    left, right = upi.split("@", 1)
    left_mask = (left[:2] + "****") if len(left) >= 2 else "****"
    return f"{left_mask}@{right}"


def _payout_method_from_request_or_user(user: User, raw: Any) -> str:
    m = (str(raw or "")).strip().upper()
    if not m:
        return (getattr(user, "payout_preference", "") or "UPI").upper()
    return m


@dataclass(frozen=True)
class WithdrawalBlock:
    blocked: bool
    reason: str | None = None
    cta: str | None = None
    message: str | None = None


def _withdrawal_block_state(user: User, *, payout_method: str | None) -> WithdrawalBlock:
    if getattr(user, "account_status", None) in (
        User.AccountStatus.SUSPENDED,
        User.AccountStatus.DEACTIVATED,
    ) or not getattr(user, "is_active", True):
        return WithdrawalBlock(
            blocked=True,
            reason="account_suspended",
            cta=None,
            message="Account is not active for withdrawals.",
        )
    if getattr(user, "kyc_status", None) != User.KYCStatus.VERIFIED:
        return WithdrawalBlock(
            blocked=True,
            reason="kyc_required",
            cta="complete_kyc",
            message="Complete KYC before requesting a withdrawal.",
        )
    method = (payout_method or (getattr(user, "payout_preference", None) or "UPI")).upper()
    if method == WithdrawalRequest.PayoutMethod.BANK:
        if not (getattr(user, "bank_account_number", "") or "").strip() or not (
            getattr(user, "bank_ifsc", "") or ""
        ).strip():
            return WithdrawalBlock(
                blocked=True,
                reason="missing_payout_method",
                cta="add_payout_method",
                message="Add your bank account details to request a withdrawal.",
            )
    else:
        if not (getattr(user, "upi_id", "") or "").strip():
            return WithdrawalBlock(
                blocked=True,
                reason="missing_payout_method",
                cta="add_payout_method",
                message="Add your UPI ID to request a withdrawal.",
            )
    return WithdrawalBlock(blocked=False)


def _payout_destination_hint(user: User, payout_method: str) -> str:
    method = (payout_method or "UPI").upper()
    if method == WithdrawalRequest.PayoutMethod.BANK:
        acct = _mask_bank_account(getattr(user, "bank_account_number", "") or "")
        ifsc = (getattr(user, "bank_ifsc", "") or "").strip().upper()
        if acct and ifsc:
            return f"{acct} ({ifsc})"
        return acct or ifsc
    return _mask_upi(getattr(user, "upi_id", "") or "")

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def user_payouts_bundle(request: Request):
    """Consolidated payouts: wallet, band ladder, withdrawals (see GET /api/v1/user/payouts/)."""
    raw = (request.query_params.get("movements") or "").strip().lower()
    include_movements = raw in ("1", "true", "yes", "on")
    return envelope_response(build_payouts_bundle(request.user, include_movements=include_movements))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wallet_me(request):
    w = get_wallet_row(request.user)
    kyc_ok = request.user.kyc_status == User.KYCStatus.VERIFIED
    cutoff = timezone.now() - timedelta(days=COOLING_DAYS)
    recent_credit = (
        WalletTransaction.objects.filter(
            user=request.user,
            tx_type=WalletTransaction.TxType.CREDIT,
            created_at__gt=cutoff,
        ).aggregate(s=Sum("amount"))["s"]
        or ZERO
    )
    locked_balance = min(w.cash_balance or ZERO, recent_credit)
    available_balance = max(ZERO, (w.cash_balance or ZERO) - locked_balance)
    return envelope_response(
        {
            "cash_balance": str(w.cash_balance),
            "available_balance": str(available_balance),
            "locked_balance": str(locked_balance),
            "cooling_days": COOLING_DAYS,
            "total_earned": str(w.total_earned),
            "current_band": w.current_band,
            "fy_withdrawn": str(w.band_cash_withdrawn_fy),
            "total_withdrawn": str(w.total_withdrawn),
            "fy_label": w.fy_label,
            "withdrawals_blocked": not kyc_ok,
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wallet_transactions(request):
    qs = WalletTransaction.objects.filter(user=request.user)[:100]
    data = [
        {
            "type": x.tx_type,
            "amount": str(x.amount),
            "balance_after": str(x.balance_after),
            "reference": x.reference,
            "at": x.created_at.isoformat(),
        }
        for x in qs
    ]
    return envelope_response({"results": data})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wallet_bands(request):
    w = get_wallet_row(request.user)
    cfg = get_system_config()
    return envelope_response(
        {"bands": describe_bands_status(w), "ladder": build_band_ladder(w, cfg)}
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def wallet_withdraw(request):
    user = request.user
    band = int(request.data.get("band", 0) or 0)
    payout_method = _payout_method_from_request_or_user(user, request.data.get("method"))
    amount = _parse_amount(request.data.get("amount"))
    if amount is None:
        return envelope_response(
            None,
            message="Invalid amount",
            success=False,
            errors={"detail": "invalid_amount"},
            status=400,
        )
    if amount < MIN_WITHDRAWAL:
        return envelope_response(
            None,
            message=f"Minimum withdrawal is ₹{MIN_WITHDRAWAL}.",
            success=False,
            errors={"detail": "min_withdrawal"},
            status=400,
        )
    if payout_method not in (WithdrawalRequest.PayoutMethod.BANK, WithdrawalRequest.PayoutMethod.UPI):
        return envelope_response(
            None,
            message="Invalid payout method.",
            success=False,
            errors={"detail": "invalid_payout_method"},
            status=400,
        )
    block = _withdrawal_block_state(user, payout_method=payout_method)
    if block.blocked:
        return envelope_response(
            {
                "withdrawals_blocked": True,
                "withdrawals_block_reason": block.reason,
                "withdrawals_block_cta": block.cta,
            },
            message=block.message or "Withdrawals blocked.",
            success=False,
            errors={"detail": block.reason},
            status=403,
        )
    if band in SLOT_BANDS:
        return envelope_response(
            None,
            message="This band is a sponsor slot band; cash withdrawal is not available.",
            success=False,
            errors={"detail": "slot_band"},
            status=400,
        )
    if band not in CASH_BANDS:
        return envelope_response(
            None,
            message="Invalid band.",
            success=False,
            errors={"detail": "invalid_band"},
            status=400,
        )

    with transaction.atomic():
        wallet, _ = Wallet.objects.select_for_update().get_or_create(user=user)
        if wallet.current_band != band:
            return envelope_response(
                None,
                message="Requested band does not match your current band.",
                success=False,
                errors={"detail": "band_mismatch", "current_band": wallet.current_band},
                status=400,
            )
        cutoff = timezone.now() - timedelta(days=COOLING_DAYS)
        recent_credit = (
            WalletTransaction.objects.filter(
                user=user,
                tx_type=WalletTransaction.TxType.CREDIT,
                created_at__gt=cutoff,
            ).aggregate(s=Sum("amount"))["s"]
            or ZERO
        )
        locked_balance = min(wallet.cash_balance or ZERO, recent_credit)
        available_balance = max(ZERO, (wallet.cash_balance or ZERO) - locked_balance)
        if amount <= ZERO or amount > available_balance:
            return envelope_response(
                None,
                message="Invalid amount",
                success=False,
                errors={"detail": "insufficient_available_balance"},
                status=400,
            )

        tds = calculate_and_apply_194h_tds(user=user, gross_amount=amount)
        tds_amount = _q2(tds.tds_amount)
        net_payable = _q2(tds.net_amount)
        wr = WithdrawalRequest.objects.create(
            user=user,
            band=band,
            amount_requested=amount,
            tds_amount=tds_amount,
            net_payable=net_payable,
            tds_section="194H" if tds_amount > ZERO else "",
            payout_method=payout_method,
            payout_destination_hint=_payout_destination_hint(user, payout_method),
        )

        wallet.cash_balance = _q2(wallet.cash_balance - amount)
        wallet.total_withdrawn = _q2(wallet.total_withdrawn + net_payable)
        wallet.total_tds_deducted = _q2(wallet.total_tds_deducted + tds_amount)
        wallet.band_cash_withdrawn_fy = _q2(wallet.band_cash_withdrawn_fy + amount)
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
            user=user,
            tx_type=WalletTransaction.TxType.DEBIT,
            amount=amount,
            balance_after=wallet.cash_balance,
            reference=f"withdrawal:{wr.id}",
            meta={
                "withdrawal_id": wr.id,
                "band": band,
                "tds_amount": str(tds_amount),
                "net_payable": str(net_payable),
                "payout_method": payout_method,
            },
        )

    return envelope_response(
        {
            "id": wr.id,
            "status": wr.status,
            "band": wr.band,
            "amount_requested": str(wr.amount_requested),
            "tds_amount": str(wr.tds_amount),
            "tds_section": wr.tds_section or None,
            "net_payable": str(wr.net_payable),
            "payout_method": wr.payout_method,
            "payout_destination_hint": wr.payout_destination_hint or None,
            "created_at": wr.created_at.isoformat(),
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wallet_withdrawals_history(request):
    raw_status = (request.query_params.get("status") or "").strip().upper()
    page = int(request.query_params.get("page", 1) or 1)
    page_size = int(request.query_params.get("page_size", 20) or 20)
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)

    date_from = parse_date((request.query_params.get("from") or "").strip())
    date_to = parse_date((request.query_params.get("to") or "").strip())

    qs = WithdrawalRequest.objects.filter(user=request.user)
    if raw_status:
        if raw_status not in set(WithdrawalRequest.Status.values):
            return envelope_response(
                None,
                message="Invalid status filter.",
                success=False,
                errors={"detail": "invalid_status"},
                status=400,
            )
        qs = qs.filter(status=raw_status)
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)

    total = qs.count()
    offset = (page - 1) * page_size
    rows = (
        qs.order_by("-id")
        .only(
            "id",
            "band",
            "amount_requested",
            "net_payable",
            "tds_amount",
            "tds_section",
            "status",
            "payout_method",
            "payout_destination_hint",
            "utr_number",
            "reject_reason",
            "approved_at",
            "paid_at",
            "created_at",
            "updated_at",
        )[offset : offset + page_size]
    )
    data = [
        {
            "id": x.id,
            "band": x.band,
            "amount_requested": str(x.amount_requested),
            "tds_amount": str(x.tds_amount),
            "tds_section": x.tds_section or None,
            "net_payable": str(x.net_payable),
            "status": x.status,
            "payout_method": x.payout_method,
            "payout_destination_hint": x.payout_destination_hint or None,
            "utr_number": x.utr_number or None,
            "reject_reason": x.reject_reason or None,
            "approved_at": x.approved_at.isoformat() if x.approved_at else None,
            "paid_at": x.paid_at.isoformat() if x.paid_at else None,
            "created_at": x.created_at.isoformat(),
            "updated_at": x.updated_at.isoformat(),
        }
        for x in rows
    ]
    return envelope_response(
        {
            "count": total,
            "page": page,
            "page_size": page_size,
            "results": data,
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wallet_withdrawals_export(request: Request):
    from django.http import HttpResponse
    import csv

    raw_status = (request.query_params.get("status") or "").strip().upper()
    date_from = parse_date((request.query_params.get("from") or "").strip())
    date_to = parse_date((request.query_params.get("to") or "").strip())

    qs = WithdrawalRequest.objects.filter(user=request.user)
    if raw_status:
        if raw_status not in set(WithdrawalRequest.Status.values):
            return envelope_response(
                None,
                message="Invalid status filter.",
                success=False,
                errors={"detail": "invalid_status"},
                status=400,
            )
        qs = qs.filter(status=raw_status)
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)

    resp = HttpResponse(content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="withdrawals.csv"'
    writer = csv.writer(resp)
    writer.writerow(
        [
            "id",
            "band",
            "status",
            "amount_requested",
            "tds_amount",
            "net_payable",
            "payout_method",
            "payout_destination_hint",
            "utr_number",
            "created_at",
            "approved_at",
            "paid_at",
        ]
    )
    for x in qs.order_by("-id").iterator():
        writer.writerow(
            [
                x.id,
                x.band,
                x.status,
                str(x.amount_requested),
                str(x.tds_amount),
                str(x.net_payable),
                x.payout_method,
                x.payout_destination_hint or "",
                x.utr_number or "",
                x.created_at.isoformat(),
                x.approved_at.isoformat() if x.approved_at else "",
                x.paid_at.isoformat() if x.paid_at else "",
            ]
        )
    return resp


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def admin_withdrawals(request):
    raw_status = (request.query_params.get("status") or "").strip().upper()
    q = (request.query_params.get("q") or "").strip()
    page = int(request.query_params.get("page", 1) or 1)
    page_size = int(request.query_params.get("page_size", 50) or 50)
    page = max(page, 1)
    page_size = min(max(page_size, 1), 200)

    date_from = parse_date((request.query_params.get("from") or "").strip())
    date_to = parse_date((request.query_params.get("to") or "").strip())

    qs = WithdrawalRequest.objects.select_related("user")
    if raw_status:
        if raw_status not in set(WithdrawalRequest.Status.values):
            return envelope_response(
                None,
                message="Invalid status filter.",
                success=False,
                errors={"detail": "invalid_status"},
                status=400,
            )
        qs = qs.filter(status=raw_status)
    if q:
        from django.db.models import Q

        qs = qs.filter(Q(user__member_id__icontains=q) | Q(user__phone__icontains=q))
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)

    total = qs.count()
    offset = (page - 1) * page_size
    rows = (
        qs.order_by("-id")
        .only(
            "id",
            "band",
            "amount_requested",
            "net_payable",
            "tds_amount",
            "tds_section",
            "status",
            "payout_method",
            "payout_destination_hint",
            "utr_number",
            "reject_reason",
            "approved_at",
            "paid_at",
            "created_at",
            "updated_at",
            "user__id",
            "user__member_id",
            "user__full_name",
            "user__phone",
            "user__kyc_status",
        )[offset : offset + page_size]
    )
    results = [
        {
            "id": x.id,
            "member": {
                "member_id": x.user.member_id,
                "full_name": x.user.full_name,
                "phone": x.user.phone,
                "kyc_status": x.user.kyc_status,
            },
            "band": x.band,
            "amount_requested": str(x.amount_requested),
            "tds_amount": str(x.tds_amount),
            "tds_section": x.tds_section or None,
            "net_payable": str(x.net_payable),
            "status": x.status,
            "payout_method": x.payout_method,
            "payout_destination_hint": x.payout_destination_hint or None,
            "utr_number": x.utr_number or None,
            "reject_reason": x.reject_reason or None,
            "approved_at": x.approved_at.isoformat() if x.approved_at else None,
            "paid_at": x.paid_at.isoformat() if x.paid_at else None,
            "created_at": x.created_at.isoformat(),
            "updated_at": x.updated_at.isoformat(),
        }
        for x in rows
    ]
    return envelope_response(
        {
            "count": total,
            "page": page,
            "page_size": page_size,
            "results": results,
        }
    )


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def admin_withdrawals_pending(request):
    qs = WithdrawalRequest.objects.filter(status=WithdrawalRequest.Status.PENDING)
    return envelope_response({"results": [x.id for x in qs]})


@api_view(["POST"])
@permission_classes([IsFinanceAdmin])
def admin_withdrawal_approve(request, pk: int):
    wr = WithdrawalRequest.objects.select_related("user").filter(pk=pk).first()
    if not wr:
        return envelope_response(None, message="Not found", success=False, status=404)
    if wr.status != WithdrawalRequest.Status.PENDING:
        return envelope_response(
            None,
            message="Withdrawal is not pending.",
            success=False,
            errors={"detail": "invalid_state", "status": wr.status},
            status=400,
        )
    wr.status = WithdrawalRequest.Status.APPROVED
    wr.approved_at = timezone.now()
    wr.approved_by = request.user
    wr.save(update_fields=["status", "approved_at", "approved_by", "updated_at"])
    return envelope_response(
        {
            "id": wr.id,
            "status": wr.status,
            "approved_at": wr.approved_at.isoformat() if wr.approved_at else None,
        }
    )


@api_view(["POST"])
@permission_classes([IsFinanceAdmin])
def admin_withdrawal_reject(request, pk: int):
    wr = WithdrawalRequest.objects.select_related("user").filter(pk=pk).first()
    if not wr:
        return envelope_response(None, message="Not found", success=False, status=404)
    if wr.status not in (WithdrawalRequest.Status.PENDING, WithdrawalRequest.Status.APPROVED):
        return envelope_response(
            None,
            message="Withdrawal cannot be rejected in this state.",
            success=False,
            errors={"detail": "invalid_state", "status": wr.status},
            status=400,
        )
    reason = (request.data.get("reason") or "").strip()
    with transaction.atomic():
        wallet, _ = Wallet.objects.select_for_update().get_or_create(user=wr.user)
        wallet.cash_balance = _q2(wallet.cash_balance + wr.amount_requested)
        wallet.total_withdrawn = _q2(wallet.total_withdrawn - wr.net_payable)
        wallet.total_tds_deducted = _q2(wallet.total_tds_deducted - wr.tds_amount)
        wallet.band_cash_withdrawn_fy = _q2(wallet.band_cash_withdrawn_fy - wr.amount_requested)
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
    return envelope_response({"id": wr.id, "status": wr.status})


@api_view(["POST"])
@permission_classes([IsFinanceAdmin])
def admin_withdrawal_mark_paid(request: Request, pk: int):
    utr = (request.data.get("utr_number") or "").strip()
    if not utr:
        return envelope_response(
            None,
            message="utr_number is required.",
            success=False,
            errors={"detail": "missing_utr"},
            status=400,
        )
    paid_at_raw = (request.data.get("paid_at") or "").strip()
    paid_at = None
    if paid_at_raw:
        try:
            paid_at = timezone.datetime.fromisoformat(paid_at_raw)
            if timezone.is_naive(paid_at):
                paid_at = timezone.make_aware(paid_at, timezone.get_current_timezone())
        except Exception:
            return envelope_response(
                None,
                message="Invalid paid_at datetime.",
                success=False,
                errors={"detail": "invalid_paid_at"},
                status=400,
            )
    wr = WithdrawalRequest.objects.filter(pk=pk).first()
    if not wr:
        return envelope_response(None, message="Not found", success=False, status=404)
    if wr.status not in (WithdrawalRequest.Status.APPROVED, WithdrawalRequest.Status.PROCESSING):
        return envelope_response(
            None,
            message="Withdrawal cannot be marked paid in this state.",
            success=False,
            errors={"detail": "invalid_state", "status": wr.status},
            status=400,
        )
    wr.status = WithdrawalRequest.Status.PAID
    wr.utr_number = utr
    wr.paid_at = paid_at or timezone.now()
    wr.paid_by = request.user
    wr.save(update_fields=["status", "utr_number", "paid_at", "paid_by", "updated_at"])
    return envelope_response(
        {
            "id": wr.id,
            "status": wr.status,
            "utr_number": wr.utr_number,
            "paid_at": wr.paid_at.isoformat() if wr.paid_at else None,
        }
    )


@api_view(["POST"])
@permission_classes([IsFinanceAdmin])
def admin_withdrawals_batch(request):
    return envelope_response({"processed": 0})


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def admin_withdrawals_export(request):
    from django.http import HttpResponse
    import csv

    raw_status = (request.query_params.get("status") or "").strip().upper()
    q = (request.query_params.get("q") or "").strip()
    date_from = parse_date((request.query_params.get("from") or "").strip())
    date_to = parse_date((request.query_params.get("to") or "").strip())

    qs = WithdrawalRequest.objects.select_related("user")
    if raw_status:
        if raw_status not in set(WithdrawalRequest.Status.values):
            return envelope_response(
                None,
                message="Invalid status filter.",
                success=False,
                errors={"detail": "invalid_status"},
                status=400,
            )
        qs = qs.filter(status=raw_status)
    if q:
        from django.db.models import Q

        qs = qs.filter(Q(user__member_id__icontains=q) | Q(user__phone__icontains=q))
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)

    resp = HttpResponse(content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="withdrawals.csv"'
    w = csv.writer(resp)
    w.writerow(
        [
            "id",
            "member_id",
            "full_name",
            "phone",
            "kyc_status",
            "band",
            "status",
            "amount_requested",
            "tds_amount",
            "net_payable",
            "payout_method",
            "payout_destination_hint",
            "utr_number",
            "created_at",
            "approved_at",
            "paid_at",
        ]
    )
    for x in qs.order_by("-id").iterator():
        w.writerow(
            [
                x.id,
                getattr(x.user, "member_id", ""),
                getattr(x.user, "full_name", ""),
                getattr(x.user, "phone", ""),
                getattr(x.user, "kyc_status", ""),
                x.band,
                x.status,
                str(x.amount_requested),
                str(x.tds_amount),
                str(x.net_payable),
                x.payout_method,
                x.payout_destination_hint or "",
                x.utr_number or "",
                x.created_at.isoformat(),
                x.approved_at.isoformat() if x.approved_at else "",
                x.paid_at.isoformat() if x.paid_at else "",
            ]
        )
    return resp
