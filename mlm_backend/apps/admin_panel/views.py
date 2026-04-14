from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes

from apps.admin_panel.models import Grievance
from apps.admin_panel.utils import get_system_config
from apps.common.permissions import (
    IsAdminRole,
    IsFinanceAdmin,
    IsSuperAdmin,
    IsSupportAdmin,
)
from apps.common.responses import envelope_response
from apps.payments.models import Order
from apps.users.models import User


@api_view(["GET"])
@permission_classes([IsAdminRole])
def dashboard(request):
    today = timezone.localdate()
    members = User.objects.filter(role=User.Role.MEMBER).count()
    orders_today = Order.objects.filter(
        status=Order.Status.PAID, paid_at__date=today
    ).count()
    return envelope_response(
        {
            "total_members": members,
            "new_orders_today": orders_today,
            "pending_withdrawals": 0,
            "pending_kyc": User.objects.filter(kyc_status=User.KYCStatus.PENDING).count(),
        }
    )


@api_view(["GET"])
@permission_classes([IsAdminRole])
def admin_users_list(request):
    qs = User.objects.all().order_by("-id")[:100]
    data = [
        {
            "id": u.id,
            "member_id": u.member_id,
            "full_name": u.full_name,
            "role": u.role,
            "account_status": u.account_status,
        }
        for u in qs
    ]
    return envelope_response({"results": data})


@api_view(["PATCH"])
@permission_classes([IsAdminRole])
def admin_users_detail(request, pk: int):
    u = User.objects.filter(pk=pk).first()
    if not u:
        return envelope_response(None, message="Not found", success=False, status=404)
    for field in ["role", "account_status", "kyc_status"]:
        if field in request.data:
            setattr(u, field, request.data[field])
    u.save()
    return envelope_response({"ok": True})


@api_view(["POST"])
@permission_classes([IsAdminRole])
def admin_user_suspend(request, pk: int):
    u = User.objects.filter(pk=pk).first()
    if not u:
        return envelope_response(None, message="Not found", success=False, status=404)
    u.account_status = User.AccountStatus.SUSPENDED
    u.save(update_fields=["account_status"])
    return envelope_response({"ok": True})


@api_view(["GET"])
@permission_classes([IsSupportAdmin])
def kyc_queue(request):
    qs = User.objects.filter(kyc_status=User.KYCStatus.PENDING)[:100]
    return envelope_response({"results": [u.id for u in qs]})


@api_view(["POST"])
@permission_classes([IsSupportAdmin])
def kyc_verify(request, pk: int):
    u = User.objects.filter(pk=pk).first()
    if not u:
        return envelope_response(None, message="Not found", success=False, status=404)
    u.kyc_status = User.KYCStatus.VERIFIED
    u.save(update_fields=["kyc_status"])
    return envelope_response({"ok": True})


@api_view(["GET"])
@permission_classes([IsAdminRole])
def users_delisted(request):
    return envelope_response({"results": []})


@api_view(["GET", "PATCH"])
@permission_classes([IsSuperAdmin])
def system_config_view(request):
    cfg = get_system_config()
    if request.method == "GET":
        return envelope_response(
            {
                "product_base_price": str(cfg.product_base_price),
                "gst_rate": str(cfg.gst_rate),
                "direct_commission": str(cfg.direct_commission),
                "upline_commission": str(cfg.upline_commission),
                "earning_cap": str(cfg.earning_cap),
            }
        )
    for field in [
        "product_base_price",
        "gst_rate",
        "direct_commission",
        "upline_commission",
        "earning_cap",
        "refund_window_days",
    ]:
        if field in request.data:
            setattr(cfg, field, request.data[field])
    cfg.updated_by = request.user
    cfg.save()
    return envelope_response({"ok": True})


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def report_tds(request):
    return envelope_response({"fy": "2026-27", "total": "0.00"})


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def report_gst(request):
    return envelope_response({"collected": "0.00"})


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def report_retail_ratio(request):
    total = Order.objects.filter(status=Order.Status.PAID).count()
    retail = Order.objects.filter(status=Order.Status.PAID, is_retail_purchase=True).count()
    ratio = (retail / total) if total else 0
    return envelope_response({"retail_ratio": ratio, "total_orders": total, "retail_orders": retail})


@api_view(["GET"])
@permission_classes([IsAdminRole])
def report_compliance(request):
    return envelope_response({"doca_ready": True})


@api_view(["GET"])
@permission_classes([IsSupportAdmin])
def grievances_list(request):
    qs = Grievance.objects.all()[:100]
    data = [{"id": g.id, "subject": g.subject, "status": g.status} for g in qs]
    return envelope_response({"results": data})


@api_view(["POST"])
@permission_classes([IsSupportAdmin])
def grievance_respond(request, pk: int):
    g = Grievance.objects.filter(pk=pk).first()
    if not g:
        return envelope_response(None, message="Not found", success=False, status=404)
    g.admin_response = request.data.get("response", "")
    g.status = Grievance.Status.CLOSED
    g.save()
    return envelope_response({"ok": True})
