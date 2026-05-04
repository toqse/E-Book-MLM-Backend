from django.db.models import F
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes

from apps.admin_panel.models import Grievance
from apps.agreements.models import MemberComplianceProfile
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


def _abs_media_url(request, filefield) -> str | None:
    if not filefield:
        return None
    try:
        return request.build_absolute_uri(filefield.url)
    except Exception:
        return filefield.url


@api_view(["GET"])
@permission_classes([IsSupportAdmin])
def compliance_queue(request):
    qs = User.objects.filter(kyc_status=User.KYCStatus.PENDING).order_by(
        F("kyc_submitted_at").desc(nulls_last=True), "-id"
    )[:100]
    out = []
    for u in qs.select_related("compliance_profile"):
        p = getattr(u, "compliance_profile", None)
        row = {
            "user_id": u.id,
            "member_id": u.member_id,
            "full_name": u.full_name,
            "phone": u.phone,
            "email": u.email,
            "kyc_submitted_at": u.kyc_submitted_at.isoformat()
            if u.kyc_submitted_at
            else None,
            "compliance_submission_version": u.compliance_submission_version,
        }
        if p:
            row["profile"] = {
                "date_of_birth": p.date_of_birth.isoformat()
                if p.date_of_birth
                else None,
                "gender": p.gender,
                "full_address": p.full_address,
                "city": p.city,
                "pin_code": p.pin_code,
                "state": p.state,
                "country": p.country,
                "pan_number": p.pan_number,
                "name_on_pan": p.name_on_pan,
                "aadhar_number": p.aadhar_number,
                "name_on_aadhar": p.name_on_aadhar,
                "nominee_name": p.nominee_name,
                "nominee_relationship": p.nominee_relationship,
                "nominee_phone": p.nominee_phone,
                "nominee_date_of_birth": p.nominee_date_of_birth.isoformat()
                if p.nominee_date_of_birth
                else None,
                "account_holder_name": p.account_holder_name,
                "bank_name": p.bank_name,
                "account_number": p.account_number,
                "ifsc": p.ifsc,
                "branch": p.branch,
                "account_type": p.account_type,
                "payout_preference": p.payout_preference,
            }
            row["pan_document_url"] = _abs_media_url(request, p.pan_document)
            row["aadhar_front_url"] = _abs_media_url(request, p.aadhar_front)
            row["aadhar_back_url"] = _abs_media_url(request, p.aadhar_back)
            row["bank_on_user"] = {
                "bank_account_number": u.bank_account_number,
                "bank_ifsc": u.bank_ifsc,
                "upi_id": u.upi_id,
            }
        else:
            row["profile"] = None
            row["pan_document_url"] = None
            row["aadhar_front_url"] = None
            row["aadhar_back_url"] = None
        out.append(row)
    return envelope_response({"results": out})


@api_view(["GET"])
@permission_classes([IsSupportAdmin])
def kyc_queue(request):
    """Deprecated: use GET /api/v1/admin/compliance-queue/ for full payload."""
    qs = User.objects.filter(kyc_status=User.KYCStatus.PENDING)[:100]
    return envelope_response({"results": [u.id for u in qs]})


@api_view(["POST"])
@permission_classes([IsSupportAdmin])
def compliance_approve(request, pk: int):
    u = User.objects.filter(pk=pk).first()
    if not u:
        return envelope_response(None, message="Not found", success=False, status=404)
    if not MemberComplianceProfile.objects.filter(user=u).exists():
        return envelope_response(
            None,
            message="Member has no compliance profile to approve.",
            success=False,
            status=400,
        )
    now = timezone.now()
    u.kyc_status = User.KYCStatus.VERIFIED
    u.kyc_reviewed_at = now
    u.kyc_rejection_reason = ""
    u.save(
        update_fields=["kyc_status", "kyc_reviewed_at", "kyc_rejection_reason", "updated_at"]
    )
    return envelope_response({"kyc_status": u.kyc_status, "kyc_reviewed_at": now.isoformat()})


@api_view(["POST"])
@permission_classes([IsSupportAdmin])
def compliance_reject(request, pk: int):
    u = User.objects.filter(pk=pk).first()
    if not u:
        return envelope_response(None, message="Not found", success=False, status=404)
    reason = (request.data.get("reason") or "").strip()
    if not reason:
        return envelope_response(
            None,
            message="reason is required",
            success=False,
            status=400,
        )
    now = timezone.now()
    u.kyc_status = User.KYCStatus.REJECTED
    u.kyc_reviewed_at = now
    u.kyc_rejection_reason = reason
    u.save(
        update_fields=["kyc_status", "kyc_reviewed_at", "kyc_rejection_reason", "updated_at"]
    )
    return envelope_response({"kyc_status": u.kyc_status})


@api_view(["POST"])
@permission_classes([IsSupportAdmin])
def kyc_verify(request, pk: int):
    """Backward-compatible alias for compliance approval."""
    return compliance_approve(request, pk)


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
                "refund_window_days": cfg.refund_window_days,
                "placement_manual_window_hours": cfg.placement_manual_window_hours,
                "auto_placement_strategy": cfg.auto_placement_strategy,
                "is_repurchase_commission_allowed": cfg.is_repurchase_commission_allowed,
            }
        )
    for field in [
        "product_base_price",
        "gst_rate",
        "direct_commission",
        "upline_commission",
        "earning_cap",
        "refund_window_days",
        "placement_manual_window_hours",
        "auto_placement_strategy",
        "is_repurchase_commission_allowed",
    ]:
        if field not in request.data:
            continue
        val = request.data[field]
        if field == "is_repurchase_commission_allowed":
            setattr(
                cfg,
                field,
                val is True or str(val).lower() in ("1", "true", "yes"),
            )
        else:
            setattr(cfg, field, val)
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
