from decimal import Decimal

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from apps.common.permissions import IsFinanceAdmin
from apps.common.responses import envelope_response

from .bands import describe_bands_status
from .models import Wallet, WalletTransaction, WithdrawalRequest


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wallet_me(request):
    w, _ = Wallet.objects.get_or_create(user=request.user)
    return envelope_response(
        {
            "cash_balance": str(w.cash_balance),
            "total_earned": str(w.total_earned),
            "current_band": w.current_band,
            "fy_withdrawn": str(w.band_cash_withdrawn_fy),
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
            "at": x.created_at.isoformat(),
        }
        for x in qs
    ]
    return envelope_response({"results": data})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wallet_bands(request):
    w, _ = Wallet.objects.get_or_create(user=request.user)
    return envelope_response({"bands": describe_bands_status(w)})


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
    qs = WithdrawalRequest.objects.filter(user=request.user).order_by("-id")[:50]
    data = [
        {
            "id": x.id,
            "band": x.band,
            "amount": str(x.amount_requested),
            "status": x.status,
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
