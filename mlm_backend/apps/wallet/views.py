from decimal import Decimal

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request

from apps.admin_panel.utils import get_system_config
from apps.common.permissions import IsFinanceAdmin
from apps.common.responses import envelope_response
from apps.users.models import User
from apps.wallet.services.member_money import (
    build_band_ladder,
    build_payouts_bundle,
    get_wallet_row,
)

from .bands import describe_bands_status
from .models import Wallet, WalletTransaction, WithdrawalRequest


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
    return envelope_response(
        {
            "cash_balance": str(w.cash_balance),
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
    band = int(request.data.get("band", 0))
    amount = Decimal(request.data.get("amount", "0"))
    w, _ = Wallet.objects.get_or_create(user=request.user)
    if amount <= 0 or amount > w.cash_balance:
        return envelope_response(None, message="Invalid amount", success=False, status=400)
    wr = WithdrawalRequest.objects.create(
        user=request.user,
        band=band,
        amount_requested=amount,
        net_payable=amount,
    )
    return envelope_response({"withdrawal_id": wr.id, "status": wr.status})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wallet_withdrawals_history(request):
    qs = (
        WithdrawalRequest.objects.filter(user=request.user)
        .order_by("-id")[:50]
        .only(
            "id",
            "band",
            "amount_requested",
            "net_payable",
            "tds_amount",
            "tds_section",
            "status",
            "created_at",
            "updated_at",
        )
    )
    data = [
        {
            "id": x.id,
            "band": x.band,
            "amount": str(x.amount_requested),
            "net_payable": str(x.net_payable),
            "tds_amount": str(x.tds_amount),
            "tds_section": x.tds_section,
            "status": x.status,
            "created_at": x.created_at.isoformat(),
            "updated_at": x.updated_at.isoformat(),
        }
        for x in qs
    ]
    return envelope_response({"results": data})


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def admin_withdrawals(request):
    qs = WithdrawalRequest.objects.all().order_by("-id")[:200]
    return envelope_response({"results": [{"id": x.id, "status": x.status} for x in qs]})


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def admin_withdrawals_pending(request):
    qs = WithdrawalRequest.objects.filter(status=WithdrawalRequest.Status.PENDING)
    return envelope_response({"results": [x.id for x in qs]})


@api_view(["POST"])
@permission_classes([IsFinanceAdmin])
def admin_withdrawal_approve(request, pk: int):
    wr = WithdrawalRequest.objects.filter(pk=pk).first()
    if not wr:
        return envelope_response(None, message="Not found", success=False, status=404)
    wr.status = WithdrawalRequest.Status.APPROVED
    wr.save(update_fields=["status"])
    return envelope_response({"id": wr.id, "status": wr.status})


@api_view(["POST"])
@permission_classes([IsFinanceAdmin])
def admin_withdrawal_reject(request, pk: int):
    wr = WithdrawalRequest.objects.filter(pk=pk).first()
    if not wr:
        return envelope_response(None, message="Not found", success=False, status=404)
    wr.status = WithdrawalRequest.Status.REJECTED
    wr.reject_reason = request.data.get("reason", "")
    wr.save(update_fields=["status", "reject_reason"])
    return envelope_response({"ok": True})


@api_view(["POST"])
@permission_classes([IsFinanceAdmin])
def admin_withdrawals_batch(request):
    return envelope_response({"processed": 0})


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def admin_withdrawals_export(request):
    from django.http import HttpResponse

    resp = HttpResponse("id,status\n", content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="withdrawals.csv"'
    return resp
