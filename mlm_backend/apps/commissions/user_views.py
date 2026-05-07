from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request

from apps.admin_panel.utils import get_system_config
from apps.common.permissions import IsFinanceAdmin, IsSuperAdmin
from apps.common.responses import envelope_response
from apps.wallet.services.member_money import (
    build_commissions_summary,
    build_earnings_response,
    get_wallet_row,
)

from .models import CommissionLedger
from .services import build_user_milestones_dashboard


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def user_earnings_bundle(request: Request):
    """Consolidated earnings: overview and/or paginated ledger (see GET /api/v1/user/earnings/)."""
    include = request.query_params.get("include")
    period = request.query_params.get("period", "all") or "all"
    typ = request.query_params.get("type", "all") or "all"
    try:
        page = max(1, int(request.query_params.get("page", "1") or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(request.query_params.get("page_size", "20") or 20)
    except (TypeError, ValueError):
        page_size = 20
    data = build_earnings_response(
        request.user,
        include_raw=include,
        period=period,
        ledger_type=typ,
        page=page,
        page_size=page_size,
    )
    return envelope_response(data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def user_commissions(request):
    qs = (
        request.user.commissions_received.select_related("source_user", "order")
        .order_by("-id")[:100]
    )
    data = [
        {
            "id": x.id,
            "type": x.commission_type,
            "amount": str(x.amount),
            "net": str(x.net_amount),
            "status": x.status,
            "created_at": x.created_at.isoformat(),
        }
        for x in qs
    ]
    return envelope_response({"results": data})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def user_commissions_summary(request: Request):
    u = request.user
    cfg = get_system_config()
    wallet = get_wallet_row(u)
    return envelope_response(build_commissions_summary(u, cfg, wallet))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def user_milestones(request):
    return envelope_response(build_user_milestones_dashboard(request.user))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def user_tds(request):
    from apps.wallet.models import Wallet

    w, _ = Wallet.objects.get_or_create(user=request.user)
    return envelope_response(
        {
            "total_tds": str(w.total_tds_deducted),
            "fy_band_cash": str(w.band_cash_withdrawn_fy),
        }
    )


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def admin_commissions(request):
    qs = CommissionLedger.objects.all().order_by("-id")[:200]
    data = [
        {
            "id": x.id,
            "recipient": x.recipient.member_id,
            "source": x.source_user.member_id,
            "type": x.commission_type,
            "net": str(x.net_amount),
        }
        for x in qs
    ]
    return envelope_response({"results": data})


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def admin_commissions_pending(request):
    qs = CommissionLedger.objects.filter(status=CommissionLedger.Status.PENDING)[:100]
    return envelope_response({"results": [x.id for x in qs]})


@api_view(["POST"])
@permission_classes([IsSuperAdmin])
def admin_force_credit(request):
    return envelope_response({"ok": False, "detail": "Not implemented in demo"})


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def admin_tds_report(request):
    return envelope_response({"month": "2026-04", "tds": "0.00"})


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def admin_commissions_export(request):
    from django.http import HttpResponse

    resp = HttpResponse("type,amount\n", content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="commissions.csv"'
    return resp
