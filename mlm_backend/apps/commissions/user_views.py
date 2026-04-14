from django.db.models import Sum
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from apps.common.permissions import IsFinanceAdmin, IsSuperAdmin
from apps.common.responses import envelope_response

from .models import CommissionLedger, MilestoneRecord


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def user_commissions(request):
    qs = request.user.commissions_received.all().order_by("-id")[:100]
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
def user_commissions_summary(request):
    u = request.user
    direct = u.commissions_received.filter(
        commission_type=CommissionLedger.CommissionType.DIRECT
    ).aggregate(s=Sum("net_amount"))["s"]
    up = u.commissions_received.filter(
        commission_type__startswith="UPLINE"
    ).aggregate(s=Sum("net_amount"))["s"]
    ms = u.milestone_records.aggregate(s=Sum("net_bonus"))["s"]
    return envelope_response(
        {
            "direct": str(direct or 0),
            "upline": str(up or 0),
            "milestone": str(ms or 0),
            "tree_passive": "0.00",
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def user_milestones(request):
    qs = MilestoneRecord.objects.filter(user=request.user)
    data = [
        {
            "referrals": x.milestone_referrals,
            "bonus": str(x.net_bonus),
            "status": x.status,
        }
        for x in qs
    ]
    return envelope_response({"history": data})


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
