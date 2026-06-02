from decimal import Decimal

from django.db import transaction
from django.db.models import F, Q, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny

from apps.admin_panel.dashboard_service import build_admin_dashboard_payload
from apps.admin_panel.models import Grievance
from apps.admin_panel.utils import get_system_config
from apps.agreements.identity_uniqueness import (
    normalize_aadhaar,
    normalize_pan,
    validate_identity_uniqueness_for_user,
)
from apps.agreements.models import MemberComplianceProfile
from apps.commissions.held_release_service import release_held_commissions_for_user
from apps.commissions.milestone_tiers import get_milestones
from apps.common.permissions import (
    IsAdminRole,
    IsFinanceAdmin,
    IsSuperAdmin,
    IsSupportAdmin,
)
from apps.common.responses import envelope_response
from apps.common.url_utils import public_absolute_uri, public_media_url
from apps.finance.services.aggregates import build_gst_report, build_tds_report_rollup
from apps.finance.services.date_range import parse_finance_range
from apps.payments.models import GSTInvoice, Order
from apps.payments.services import ensure_gst_invoice_pdf
from apps.users.models import AccountDeletionRequest, User
from apps.users.services import effective_company_referral_code, environment_company_referral_code
from apps.wallet.models import Wallet
from apps.wallet.services.member_money import build_withdrawn_block


def _coerce_int_list(val) -> list[int]:
    if val is None:
        return []
    if isinstance(val, (list, tuple)):
        raw = list(val)
    else:
        raw = [val]
    out: list[int] = []
    for x in raw:
        try:
            out.append(int(x))
        except Exception:
            continue
    return out


def _parse_positive_int(raw, default: int, *, min_v: int = 1, max_v: int = 100) -> int:
    try:
        n = int(str(raw).strip())
    except Exception:
        n = default
    n = default if n <= 0 else n
    return max(min_v, min(n, max_v))


def _parse_bool(raw) -> bool | None:
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return None


def _clean_opt_str(val, *, lower: bool = False) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    return s.lower() if lower else s


def _invoice_pdf_url(request, inv: GSTInvoice) -> str | None:
    """Best-effort absolute URL; never raises."""
    legacy = (getattr(inv, "pdf_url", "") or "").strip() or None

    names = getattr(getattr(inv, "pdf_file", None), "name", "") or ""
    if not names.strip():
        return legacy

    try:
        rel_url = inv.pdf_file.url
    except Exception:
        return legacy

    try:
        absolute = public_absolute_uri(request, rel_url)
    except Exception:
        return rel_url or legacy
    return absolute or rel_url or legacy


def _approve_compliance_by_user_ids(user_ids: list[int]):
    """
    Bulk-safe approval helper.

    Returns (approved_ids, failed_rows, reviewed_at_iso).
    """
    ids = [i for i in dict.fromkeys(user_ids) if i > 0]
    if not ids:
        return [], [{"id": None, "reason": "No user ids provided."}], None

    users = {u.id: u for u in User.objects.filter(id__in=ids)}
    existing_ids = set(users.keys())

    missing_ids = [i for i in ids if i not in existing_ids]
    profile_qs = MemberComplianceProfile.objects.filter(user_id__in=existing_ids).only(
        "user_id",
        "pan_number",
        "aadhar_number",
        "pan_document",
        "aadhar_front",
        "aadhar_back",
    )
    profiles_by_user = {p.user_id: p for p in profile_qs}
    profiles = set(profiles_by_user.keys())

    failed: list[dict] = []
    for mid in missing_ids:
        failed.append({"id": mid, "reason": "Not found"})
    for uid in ids:
        if uid in existing_ids and uid not in profiles:
            failed.append({"id": uid, "reason": "Member has no compliance profile to approve."})

    def _has_min_docs(p: MemberComplianceProfile) -> tuple[bool, str | None]:
        pan_ok = bool((p.pan_number or "").strip()) and bool(getattr(p.pan_document, "name", "") or "")
        aad_ok = bool((p.aadhar_number or "").strip()) and bool(getattr(p.aadhar_front, "name", "") or "") and bool(
            getattr(p.aadhar_back, "name", "") or ""
        )
        if not pan_ok and not aad_ok:
            return False, "Missing PAN and Aadhaar minimum documents."
        if not pan_ok:
            return False, "Missing PAN number and/or PAN document."
        if not aad_ok:
            return False, "Missing Aadhaar number and/or Aadhaar front/back documents."
        return True, None

    ok_ids: list[int] = []
    for uid in ids:
        if uid not in existing_ids or uid not in profiles_by_user:
            continue
        ok, reason = _has_min_docs(profiles_by_user[uid])
        if not ok:
            failed.append({"id": uid, "reason": reason})
            continue
        ok_ids.append(uid)
    if not ok_ids:
        return [], failed, None

    now = timezone.now()
    with transaction.atomic():
        # Snapshot which approvals are re-approvals (already had kyc_first_approved_at)
        # so we only auto-release backlog for them. First-time approvers' pre-approval
        # HELD rows are forfeited and must NOT be retroactively credited.
        reapproved_ids = list(
            User.objects.filter(
                id__in=ok_ids, kyc_first_approved_at__isnull=False
            ).values_list("id", flat=True)
        )
        User.objects.filter(id__in=ok_ids).update(
            kyc_status=User.KYCStatus.VERIFIED,
            kyc_reviewed_at=now,
            kyc_first_approved_at=Coalesce(F("kyc_first_approved_at"), Value(now)),
            kyc_rejection_reason="",
            updated_at=now,
        )
        for uid in reapproved_ids:
            transaction.on_commit(
                lambda user_id=uid: release_held_commissions_for_user(user_id=user_id, actor=None)
            )
    return ok_ids, failed, now.isoformat()


@api_view(["GET"])
@permission_classes([IsAdminRole])
def dashboard(request):
    return envelope_response(build_admin_dashboard_payload(request.query_params))


@api_view(["GET"])
@permission_classes([IsAdminRole])
def admin_users_list(request):
    page = _parse_positive_int(request.query_params.get("page"), 1, min_v=1, max_v=1_000_000)
    page_size = _parse_positive_int(
        request.query_params.get("page_size"), 20, min_v=1, max_v=100
    )

    q = (request.query_params.get("q") or "").strip()
    account_status = (request.query_params.get("account_status") or "").strip().upper() or None
    kyc_status = (request.query_params.get("kyc_status") or "").strip().upper() or None
    state = (request.query_params.get("state") or "").strip() or None
    band = request.query_params.get("band")
    band_int = _parse_positive_int(band, 0, min_v=0, max_v=1000) if band else None
    has_cap_reached = _parse_bool(request.query_params.get("has_cap_reached"))

    cfg = get_system_config()
    cap = cfg.earning_cap

    base_qs = (
        User.objects.all()
        .select_related("compliance_profile", "wallet")
        .order_by("-id")
    )

    if q:
        base_qs = base_qs.filter(
            Q(full_name__icontains=q)
            | Q(member_id__icontains=q)
            | Q(phone__icontains=q)
            | Q(email__icontains=q)
        )

    if state:
        base_qs = base_qs.filter(compliance_profile__state__iexact=state)

    if band_int is not None and band_int > 0:
        base_qs = base_qs.filter(wallet__current_band=band_int)

    if has_cap_reached is not None:
        if has_cap_reached:
            base_qs = base_qs.filter(wallet__total_earned__gte=cap)
        else:
            base_qs = base_qs.filter(Q(wallet__total_earned__lt=cap) | Q(wallet__isnull=True))

    tab_counts = {
        "all": base_qs.count(),
        "kyc_pending": base_qs.filter(kyc_status=User.KYCStatus.PENDING).count(),
        "capped": base_qs.filter(account_status=User.AccountStatus.CAPPED).count(),
        "suspended": base_qs.filter(account_status=User.AccountStatus.SUSPENDED).count(),
    }

    qs = base_qs
    if account_status:
        qs = qs.filter(account_status=account_status)
    if kyc_status:
        qs = qs.filter(kyc_status=kyc_status)

    total_count = qs.count()
    total_pages = (total_count + page_size - 1) // page_size if total_count else 0
    start = (page - 1) * page_size
    page_objs = list(qs[start : start + page_size])

    results: list[dict] = []
    for u in page_objs:
        p: MemberComplianceProfile | None = getattr(u, "compliance_profile", None)
        w: Wallet | None = getattr(u, "wallet", None)
        earned = getattr(w, "total_earned", 0) if w else 0
        used_percent = float((earned / cap) * 100) if cap and cap > 0 else 0.0
        results.append(
            {
                "id": u.id,
                "member_id": u.member_id,
                "full_name": u.full_name,
                "email": u.email,
                "phone": u.phone,
                "address": (p.full_address if p else "") or None,
                "city": (p.city if p else "") or None,
                "pincode": (p.pin_code if p else "") or None,
                "state": (p.state if p else "") or None,
                "kyc_status": u.kyc_status,
                "account_status": u.account_status,
                "band": getattr(w, "current_band", None) if w else None,
                "cap_progress": {
                    "used": str(earned),
                    "total": str(cap),
                    "percent": round(used_percent, 2),
                },
                "earnings": str(earned),
                "refs": u.direct_referral_count,
            }
        )

    return envelope_response(
        {
            "results": results,
            "count": total_count,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "tab_counts": tab_counts,
        }
    )


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAdminRole])
def admin_users_detail(request, pk: int):
    u = User.objects.filter(pk=pk).first()
    if not u:
        return envelope_response(None, message="Not found", success=False, status=404)
    if request.method == "GET":
        cfg = get_system_config()
        cap = cfg.earning_cap
        u = (
            User.objects.select_related("compliance_profile", "wallet")
            .filter(pk=pk)
            .first()
            or u
        )
        p: MemberComplianceProfile | None = getattr(u, "compliance_profile", None)
        w: Wallet | None = getattr(u, "wallet", None)
        earned = getattr(w, "total_earned", 0) if w else 0
        used_percent = float((earned / cap) * 100) if cap and cap > 0 else 0.0

        orders_qs = (
            Order.objects.filter(user_id=u.id)
            .select_related("ebook", "gst_invoice")
            .prefetch_related("lines__ebook")
            .order_by("-id")
        )
        # Safety cap to prevent extremely large responses.
        orders = list(orders_qs[:200])
        orders_truncated = orders_qs.count() > 200

        orders_out: list[dict] = []
        for o in orders:
            inv: GSTInvoice | None = getattr(o, "gst_invoice", None)
            pdf_url = None
            invoice_number = None
            if o.status == Order.Status.PAID and inv:
                invoice_number = inv.invoice_number
                try:
                    ensure_gst_invoice_pdf(o)
                    inv.refresh_from_db()
                except Exception:
                    pass
                pdf_url = _invoice_pdf_url(request, inv)

            ebooks = []
            if getattr(o, "ebook_id", None) and o.ebook:
                ebooks = [{"id": o.ebook_id, "title": o.ebook.title, "slug": o.ebook.slug}]
            else:
                for ln in list(getattr(o, "lines", []).all()):
                    eb = getattr(ln, "ebook", None)
                    if not eb:
                        continue
                    ebooks.append({"id": eb.id, "title": eb.title, "slug": eb.slug})

            orders_out.append(
                {
                    "id": o.id,
                    "order_number": o.order_number,
                    "status": o.status,
                    "amount_paid": str(o.amount_paid),
                    "base_price": str(o.base_price),
                    "gst_amount": str(o.gst_amount),
                    "gateway_charge": str(o.gateway_charge),
                    "discount_amount": str(o.discount_amount),
                    "total_amount": str(o.total_amount),
                    "is_retail_purchase": o.is_retail_purchase,
                    "is_sponsor_slot_redemption": o.is_sponsor_slot_redemption,
                    "paid_at": o.paid_at.isoformat() if o.paid_at else None,
                    "created_at": o.created_at.isoformat() if o.created_at else None,
                    "placement_status": o.placement_status,
                    "placement_leg_requested": o.placement_leg_requested,
                    "placement_resolved_at": o.placement_resolved_at.isoformat()
                    if o.placement_resolved_at
                    else None,
                    "gst_invoice": (
                        {"invoice_number": invoice_number, "pdf_url": pdf_url}
                        if invoice_number or pdf_url
                        else None
                    ),
                    "ebooks": ebooks,
                }
            )

        return envelope_response(
            {
                "id": u.id,
                "member_id": u.member_id,
                "full_name": u.full_name,
                "email": u.email,
                "phone": u.phone,
                "role": u.role,
                "is_active": u.is_active,
                "status": {
                    "kyc_status": u.kyc_status,
                    "account_status": u.account_status,
                    "kyc_submitted_at": u.kyc_submitted_at.isoformat()
                    if u.kyc_submitted_at
                    else None,
                    "kyc_reviewed_at": u.kyc_reviewed_at.isoformat()
                    if u.kyc_reviewed_at
                    else None,
                    "kyc_rejection_reason": u.kyc_rejection_reason or None,
                },
                "direct_referral_count": u.direct_referral_count,
                "personal_details": {
                    "full_name": u.full_name,
                    "email": u.email,
                    "phone": u.phone,
                    "member_id": u.member_id,
                    "referral_code": u.referral_code,
                    "pan_number": u.pan_number or None,
                    "aadhaar_number": u.aadhaar_number or None,
                },
                "compliance_kyc_details": (
                    {
                        "date_of_birth": _fmt_ddmmyyyy(p.date_of_birth) if p else None,
                        "gender": p.gender if p else None,
                        "full_address": p.full_address if p else None,
                        "city": p.city if p else None,
                        "pin_code": p.pin_code if p else None,
                        "state": p.state if p else None,
                        "country": p.country if p else None,
                        "pan_number": p.pan_number if p else None,
                        "name_on_pan": p.name_on_pan if p else None,
                        "aadhar_number": p.aadhar_number if p else None,
                        "name_on_aadhar": p.name_on_aadhar if p else None,
                        "nominee_name": p.nominee_name if p else None,
                        "nominee_relationship": p.nominee_relationship if p else None,
                        "nominee_phone": p.nominee_phone if p else None,
                        "nominee_date_of_birth": _fmt_ddmmyyyy(p.nominee_date_of_birth)
                        if p
                        else None,
                        "bank": {
                            "account_holder_name": p.account_holder_name if p else None,
                            "account_number": p.account_number if p else None,
                            "bank_name": p.bank_name if p else None,
                            "ifsc": p.ifsc if p else None,
                            "branch": p.branch if p else None,
                            "account_type": p.account_type if p else None,
                            "payout_preference": p.payout_preference if p else None,
                        }
                        if p
                        else None,
                        "documents": {
                            "pan_document_url": _abs_media_url(request, p.pan_document)
                            if p
                            else None,
                            "aadhar_front_url": _abs_media_url(request, p.aadhar_front)
                            if p
                            else None,
                            "aadhar_back_url": _abs_media_url(request, p.aadhar_back)
                            if p
                            else None,
                            "aadhar_document_url": _abs_media_url(request, p.aadhar_document)
                            if p
                            else None,
                        }
                        if p
                        else None,
                    }
                    if p
                    else None
                ),
                "earnings": {
                    "wallet": (
                        {
                            "current_band": w.current_band,
                            "total_earned": str(w.total_earned),
                            "cash_balance": str(w.cash_balance),
                            "total_withdrawn": build_withdrawn_block(w),
                            "total_tds_deducted": str(w.total_tds_deducted),
                            "updated_at": w.updated_at.isoformat() if w.updated_at else None,
                        }
                        if w
                        else None
                    ),
                    "earning_cap": {
                        "limit": str(cap),
                        "used": str(earned),
                        "remaining": str(max(0, cap - earned)) if cap and cap > 0 else "0",
                        "used_percent": round(used_percent, 2),
                        "is_capped": bool(cap and cap > 0 and earned >= cap),
                    },
                },
                "orders": {
                    "results": orders_out,
                    "count": orders_qs.count(),
                    "truncated": orders_truncated,
                },
            }
        )
    if request.method == "DELETE":
        if getattr(u, "is_staff", False) or getattr(u, "role", None) != User.Role.MEMBER:
            return envelope_response(
                None,
                message="Only member accounts can be deleted.",
                success=False,
                status=403,
            )
        AccountDeletionRequest.objects.filter(
            user=u,
            status=AccountDeletionRequest.Status.PENDING,
        ).update(
            status=AccountDeletionRequest.Status.COMPLETED,
            completed_at=timezone.now(),
            completed_by=request.user,
        )
        u.delete()
        return envelope_response({"ok": True})

    # PATCH: allow updating status + personal + compliance fields.
    data = request.data or {}

    # Status fields on User
    for field in ["role", "account_status", "kyc_status"]:
        if field in data:
            setattr(u, field, data[field])

    # Personal fields on User (limited set; identity fields like member_id/referral_code are immutable)
    if "full_name" in data:
        u.full_name = _clean_opt_str(data.get("full_name")) or u.full_name

    if "email" in data:
        new_email = _clean_opt_str(data.get("email"), lower=True)
        if new_email and User.objects.filter(email=new_email).exclude(pk=u.pk).exists():
            return envelope_response(
                None,
                message="Email already in use",
                success=False,
                errors={"email": "already_exists"},
                status=400,
            )
        u.email = new_email

    if "phone" in data:
        new_phone = _clean_opt_str(data.get("phone"))
        if new_phone and User.objects.filter(phone=new_phone).exclude(pk=u.pk).exists():
            return envelope_response(
                None,
                message="Phone already in use",
                success=False,
                errors={"phone": "already_exists"},
                status=400,
            )
        u.phone = new_phone

    for field in [
        "pan_number",
        "aadhaar_number",
        "bank_account_number",
        "bank_ifsc",
        "bank_name",
        "upi_id",
    ]:
        if field in data:
            raw = _clean_opt_str(data.get(field)) or ""
            if field == "pan_number":
                setattr(u, field, normalize_pan(raw) or None)
            elif field == "aadhaar_number":
                setattr(u, field, normalize_aadhaar(raw) or None)
            else:
                setattr(u, field, raw)

    # Compliance profile fields (create profile if any compliance field is being edited)
    compliance_fields = {
        "date_of_birth",
        "gender",
        "full_address",
        "city",
        "pin_code",
        "state",
        "country",
        "pan_number",
        "name_on_pan",
        "aadhar_number",
        "name_on_aadhar",
        "nominee_name",
        "nominee_relationship",
        "nominee_phone",
        "nominee_date_of_birth",
        "account_holder_name",
        "account_number",
        "bank_name",
        "ifsc",
        "branch",
        "account_type",
        "payout_preference",
    }
    wants_profile = any(k in data for k in compliance_fields)
    profile = None
    if wants_profile:
        profile, _ = MemberComplianceProfile.objects.get_or_create(user=u)
        # Note: date fields are accepted as ISO (YYYY-MM-DD); we keep it simple and let Django coerce
        # via assignment where possible, otherwise keep existing values.
        for k in compliance_fields:
            if k not in data:
                continue
            v = data.get(k)
            if k in ("date_of_birth", "nominee_date_of_birth") and v in (None, ""):
                setattr(profile, k, None)
                continue
            if isinstance(v, str):
                v2 = v.strip()
                if k == "pan_number":
                    v2 = normalize_pan(v2) or ""
                elif k == "aadhar_number":
                    v2 = normalize_aadhaar(v2) or ""
                setattr(profile, k, v2)
            else:
                setattr(profile, k, v)

    effective_pan = None
    if profile is not None and (profile.pan_number or "").strip():
        effective_pan = normalize_pan(profile.pan_number)
    elif (u.pan_number or "").strip():
        effective_pan = normalize_pan(u.pan_number)

    effective_aadhaar = None
    if profile is not None and (profile.aadhar_number or "").strip():
        effective_aadhaar = normalize_aadhaar(profile.aadhar_number)
    elif (u.aadhaar_number or "").strip():
        effective_aadhaar = normalize_aadhaar(u.aadhaar_number)

    identity_errors = validate_identity_uniqueness_for_user(
        pan=effective_pan,
        aadhaar=effective_aadhaar,
        user_id=u.pk,
    )
    if identity_errors:
        return envelope_response(
            None,
            message="PAN or Aadhaar already linked to another account.",
            success=False,
            errors=identity_errors,
            status=400,
        )

    u.save()
    if profile:
        profile.save()
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


@api_view(["POST"])
@permission_classes([IsAdminRole])
def admin_user_unsuspend(request, pk: int):
    u = User.objects.filter(pk=pk).first()
    if not u:
        return envelope_response(None, message="Not found", success=False, status=404)
    if u.account_status == User.AccountStatus.SUSPENDED:
        u.account_status = User.AccountStatus.ACTIVE
        u.save(update_fields=["account_status"])
    return envelope_response({"ok": True})


def _abs_media_url(request, filefield) -> str | None:
    return public_media_url(request, filefield)


def _fmt_ddmmyyyy(date_obj):
    if not date_obj:
        return None
    return date_obj.strftime("%d/%m/%Y")


@api_view(["GET"])
@permission_classes([IsSupportAdmin])
def compliance_queue(request):
    page = _parse_positive_int(
        request.query_params.get("page"), 1, min_v=1, max_v=1_000_000
    )
    page_size = _parse_positive_int(
        request.query_params.get("page_size"), 20, min_v=1, max_v=100
    )

    qs = User.objects.filter(
        kyc_status=User.KYCStatus.PENDING,
        kyc_submitted_at__isnull=False,
    ).order_by(F("kyc_submitted_at").desc(nulls_last=True), "-id")

    total_count = qs.count()
    total_pages = (total_count + page_size - 1) // page_size if total_count else 0
    start = (page - 1) * page_size
    page_qs = qs.select_related("compliance_profile")[start : start + page_size]

    out = []
    for u in page_qs:
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
                "date_of_birth": _fmt_ddmmyyyy(p.date_of_birth),
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
                "nominee_date_of_birth": _fmt_ddmmyyyy(p.nominee_date_of_birth),
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
                "bank_name": getattr(u, "bank_name", None),
                "upi_id": u.upi_id,
            }
        else:
            row["profile"] = None
            row["pan_document_url"] = None
            row["aadhar_front_url"] = None
            row["aadhar_back_url"] = None
        out.append(row)
    return envelope_response(
        {
            "results": out,
            "count": total_count,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }
    )


@api_view(["GET"])
@permission_classes([IsSupportAdmin])
def kyc_queue(request):
    """Deprecated: use GET /api/v1/admin/compliance-queue/ for full payload."""
    qs = User.objects.filter(kyc_status=User.KYCStatus.PENDING)[:100]
    return envelope_response({"results": [u.id for u in qs]})


@api_view(["POST"])
@permission_classes([IsSupportAdmin])
def compliance_approve(request, pk: int | None = None):
    """
    Approve KYC/compliance for one or many users.

    Body supports either:
      - { "user_id": 123 }
      - { "user_ids": [123, 456] }
    When body omits ids, falls back to URL param `pk` (backward compatible).
    """
    body_ids = _coerce_int_list(request.data.get("user_ids"))
    if not body_ids:
        body_ids = _coerce_int_list(request.data.get("user_id"))
    if not body_ids and pk is not None:
        body_ids = [int(pk)]

    approved_ids, failed, reviewed_at = _approve_compliance_by_user_ids(body_ids)
    unique_requested_ids = [i for i in dict.fromkeys(body_ids) if i]

    # Preserve legacy single-user semantics ONLY when URL `pk` is used.
    # The bulk-friendly endpoint (no `pk`) should behave consistently even for a single id.
    is_single_legacy = pk is not None and len(unique_requested_ids) == 1
    if is_single_legacy and not approved_ids:
        if failed and failed[0]["reason"] == "Not found":
            return envelope_response(None, message="Not found", success=False, status=404)
        msg = failed[0]["reason"] if failed else "Invalid request"
        return envelope_response(None, message=msg, success=False, status=400)

    # For bulk flows, we include per-id failures in `data.failed` and also return a
    # high-level error string in `errors.detail` for UIs that only read `errors`.
    if not approved_ids and failed:
        # If request was "single-user style" (even via the bulk endpoint), return the specific reason.
        if len(unique_requested_ids) == 1:
            message = failed[0]["reason"]
            status = 404 if message == "Not found" else 400
        else:
            message = "No users approved"
            status = 400
        return envelope_response(
            {
                "approved_ids": approved_ids,
                "failed": failed,
                "kyc_reviewed_at": reviewed_at,
            },
            success=False,
            message=message,
            errors={"detail": message},
            status=status,
        )

    message = "Approved" if len(failed) == 0 else "Approved with some failures"
    success = len(failed) == 0
    return envelope_response(
        {
            "approved_ids": approved_ids,
            "failed": failed,
            "kyc_reviewed_at": reviewed_at,
        },
        success=success,
        message=message,
        errors=None if success else {"detail": message},
    )


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


def _parse_admin_bool(val) -> bool:
    return val is True or str(val).lower() in ("1", "true", "yes")


@api_view(["POST"])
@permission_classes([IsSupportAdmin])
def admin_kyc_send_invitation(request, pk: int | None = None):
    """
    Manually send or resend the post-refund KYC invitation email/SMS.

    Body:
      - user_id (int) or user_ids (list[int])
      - force (bool, default false): resend even if kyc_invitation_sent_at is set
      - skip_refund_check (bool, default false): allow send before refund window ends
    When body omits ids, uses URL <pk> (single user).
    """
    from apps.users.kyc_invitation_service import send_kyc_invitation_to_user

    body_ids = _coerce_int_list(request.data.get("user_ids"))
    if not body_ids:
        body_ids = _coerce_int_list(request.data.get("user_id"))
    if not body_ids and pk is not None:
        body_ids = [int(pk)]

    if not body_ids:
        return envelope_response(
            None,
            message="user_id or user_ids is required",
            success=False,
            errors={"detail": "user_ids_required"},
            status=400,
        )

    force = _parse_admin_bool(request.data.get("force"))
    skip_refund_check = _parse_admin_bool(request.data.get("skip_refund_check"))

    sent_rows: list[dict] = []
    failed: list[dict] = []
    for uid in dict.fromkeys(body_ids):
        row = send_kyc_invitation_to_user(
            uid, force=force, skip_refund_check=skip_refund_check
        )
        if row.get("sent"):
            sent_rows.append(row)
        else:
            failed.append(row)

    unique_count = len(dict.fromkeys(body_ids))
    is_single_legacy = pk is not None and unique_count == 1

    if is_single_legacy and not sent_rows:
        row = failed[0] if failed else {"reason": "unknown"}
        reason = row.get("reason") or "unknown"
        status = 404 if reason == "not_found" else 400
        return envelope_response(
            row,
            success=False,
            message=reason.replace("_", " ").title(),
            errors={"detail": reason},
            status=status,
        )

    if not sent_rows and failed:
        message = "No invitations sent"
        if unique_count == 1:
            message = (failed[0].get("reason") or "unknown").replace("_", " ")
        return envelope_response(
            {"sent": sent_rows, "failed": failed},
            success=False,
            message=message.title(),
            errors={"detail": failed[0].get("reason") if len(failed) == 1 else message},
            status=400,
        )

    message = "Invitation sent" if len(failed) == 0 else "Sent with some failures"
    success = len(failed) == 0
    payload = {"sent": sent_rows, "failed": failed}
    if is_single_legacy and sent_rows:
        payload = sent_rows[0]
    return envelope_response(
        payload,
        success=success,
        message=message,
        errors=None if success else {"detail": message},
    )


def _serialize_account_deletion_request(row: AccountDeletionRequest) -> dict:
    u = row.user
    completed_by = row.completed_by
    return {
        "id": row.id,
        "user_id": u.id if u else None,
        "member_id": (u.member_id if u else None) or row.snapshot_member_id,
        "full_name": (u.full_name if u else None) or row.snapshot_full_name,
        "email": (u.email if u else None) or row.snapshot_email,
        "phone": (u.phone if u else None) or row.snapshot_phone,
        "reason": row.reason,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "completed_by": (
            {"id": completed_by.id, "full_name": completed_by.full_name}
            if completed_by
            else None
        ),
        "user_already_deleted": u is None,
    }


@api_view(["GET"])
@permission_classes([IsAdminRole])
def admin_account_deletion_requests(request):
    qs = AccountDeletionRequest.objects.select_related("user", "completed_by").order_by(
        "-created_at", "-id"
    )

    status_filter = (request.query_params.get("status") or "").strip().upper()
    if status_filter:
        if status_filter not in AccountDeletionRequest.Status.values:
            return envelope_response(
                None,
                message="Invalid status filter",
                success=False,
                errors={"status": ["Must be PENDING or COMPLETED"]},
                status=400,
            )
        qs = qs.filter(status=status_filter)

    q = (request.query_params.get("q") or "").strip()
    if q:
        qs = qs.filter(
            Q(snapshot_member_id__icontains=q)
            | Q(snapshot_full_name__icontains=q)
            | Q(snapshot_email__icontains=q)
            | Q(snapshot_phone__icontains=q)
            | Q(user__member_id__icontains=q)
            | Q(user__full_name__icontains=q)
            | Q(user__email__icontains=q)
            | Q(user__phone__icontains=q)
        )

    page = _parse_positive_int(request.query_params.get("page"), 1, min_v=1, max_v=1_000_000)
    page_size = _parse_positive_int(
        request.query_params.get("page_size"), 20, min_v=1, max_v=100
    )
    total_count = qs.count()
    total_pages = (total_count + page_size - 1) // page_size if total_count else 0
    start = (page - 1) * page_size
    rows = list(qs[start : start + page_size])

    return envelope_response(
        {
            "results": [_serialize_account_deletion_request(r) for r in rows],
            "count": total_count,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }
    )


@api_view(["GET"])
@permission_classes([IsAdminRole])
def users_delisted(request):
    return envelope_response({"results": []})


@api_view(["GET", "PATCH"])
@permission_classes([IsSuperAdmin])
def system_config_view(request):
    cfg = get_system_config()
    if request.method == "GET":
        milestones = get_milestones(cfg)
        return envelope_response(
            {
                "product_base_price": str(cfg.product_base_price),
                "gst_rate": str(cfg.gst_rate),
                "direct_commission": str(cfg.direct_commission),
                "upline_commission": str(cfg.upline_commission),
                "earning_cap": str(cfg.earning_cap),
                "refund_window_days": cfg.refund_window_days,
                "cooling_off_days": cfg.cooling_off_days,
                "refund_request_sla_hours": int(cfg.refund_request_sla_hours or 0),
                "placement_manual_window_hours": cfg.placement_manual_window_hours,
                "auto_placement_strategy": cfg.auto_placement_strategy,
                "is_repurchase_commission_allowed": cfg.is_repurchase_commission_allowed,
                "auto_process_milestone_bonuses": cfg.auto_process_milestone_bonuses,
                "trigger_instant_kyc_submission": cfg.trigger_instant_kyc_submission,
                "milestone_bonus_overrides": cfg.milestone_bonus_overrides or {},
                "milestones": [
                    {
                        "label": f"T{idx}",
                        "index": idx,
                        "threshold": int(th),
                        "bonus_gross": str(bonus),
                    }
                    for idx, (th, _pct, bonus) in enumerate(milestones, start=1)
                ],
                "razorpay": {
                    "key_id": cfg.razorpay_key_id or None,
                    "key_secret_set": bool((cfg.razorpay_key_secret or "").strip()),
                },
                "grievance_nodal_officer": {
                    "nodal_officer_name": cfg.nodal_officer_name or "",
                    "nodal_officer_email": cfg.nodal_officer_email or "",
                    "nodal_officer_phone": cfg.nodal_officer_phone or "",
                    "grievance_sla_hours": int(cfg.grievance_sla_hours or 0),
                },
                "default_company_referral_code": effective_company_referral_code(),
                "default_company_referral_code_environment": environment_company_referral_code(),
                "default_company_referral_code_override": (cfg.default_company_referral_code or "").strip()
                or None,
                "app_version": {
                    "latest_app_version": cfg.latest_app_version or "",
                    "force_update": bool(cfg.force_update),
                },
            }
        )
    data = request.data or {}
    errors: dict[str, str] = {}

    decimal_fields = {
        "product_base_price",
        "gst_rate",
        "direct_commission",
        "upline_commission",
        "earning_cap",
    }
    int_fields = {
        "refund_window_days",
        "cooling_off_days",
        "refund_request_sla_hours",
        "placement_manual_window_hours",
        "grievance_sla_hours",
    }
    for field in [
        "product_base_price",
        "gst_rate",
        "direct_commission",
        "upline_commission",
        "earning_cap",
        "refund_window_days",
        "cooling_off_days",
        "refund_request_sla_hours",
        "placement_manual_window_hours",
        "auto_placement_strategy",
        "is_repurchase_commission_allowed",
        "auto_process_milestone_bonuses",
        "trigger_instant_kyc_submission",
        "milestone_bonus_overrides",
        "razorpay_key_id",
        "razorpay_key_secret",
        "nodal_officer_name",
        "nodal_officer_email",
        "nodal_officer_phone",
        "grievance_sla_hours",
        "default_company_referral_code",
        "latest_app_version",
        "force_update",
    ]:
        if field not in data:
            continue
        val = data[field]
        if field == "latest_app_version":
            if val in (None, ""):
                cfg.latest_app_version = ""
            elif isinstance(val, str):
                s = val.strip()
                if len(s) > 32:
                    errors[field] = "max_length_32"
                    continue
                cfg.latest_app_version = s
            else:
                errors[field] = "must_be_string"
            continue
        if field == "default_company_referral_code":
            if val in (None, ""):
                cfg.default_company_referral_code = ""
            elif isinstance(val, str):
                s = val.strip()
                if len(s) > 64:
                    errors[field] = "max_length_64"
                    continue
                cfg.default_company_referral_code = s
            else:
                errors[field] = "must_be_string"
            continue
        if field in (
            "is_repurchase_commission_allowed",
            "auto_process_milestone_bonuses",
            "trigger_instant_kyc_submission",
            "force_update",
        ):
            setattr(
                cfg,
                field,
                val is True or str(val).lower() in ("1", "true", "yes"),
            )
        elif field == "milestone_bonus_overrides":
            if isinstance(val, dict):
                cfg.milestone_bonus_overrides = val
            elif val in (None, ""):
                cfg.milestone_bonus_overrides = {}
            else:
                errors[field] = "must_be_object"
        elif field in decimal_fields:
            if val in (None, ""):
                errors[field] = "required_decimal"
                continue
            try:
                setattr(cfg, field, Decimal(str(val)))
            except Exception:
                errors[field] = "invalid_decimal"
        elif field in int_fields:
            if val in (None, ""):
                errors[field] = "required_int"
                continue
            try:
                ival = int(str(val).strip())
                if ival < 0:
                    raise ValueError("negative")
                setattr(cfg, field, ival)
            except Exception:
                errors[field] = "invalid_int"
        else:
            setattr(cfg, field, val)
    if errors:
        return envelope_response(
            None,
            success=False,
            message="Invalid config payload",
            errors=errors,
            status=400,
        )
    cfg.updated_by = request.user
    cfg.save()
    return envelope_response({"ok": True})


@api_view(["GET"])
@permission_classes([AllowAny])
def public_app_version(request):
    cfg = get_system_config()
    return envelope_response(
        {
            "latest_app_version": cfg.latest_app_version or "",
            "force_update": bool(cfg.force_update),
        }
    )


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def report_tds(request):
    fr = parse_finance_range(request.query_params)
    return envelope_response(build_tds_report_rollup(fr))


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def report_gst(request):
    fr = parse_finance_range(request.query_params)
    page = _parse_positive_int(request.query_params.get("page"), 1, min_v=1, max_v=1_000_000)
    page_size = _parse_positive_int(
        request.query_params.get("page_size"), 20, min_v=1, max_v=100
    )
    return envelope_response(build_gst_report(fr, page=page, page_size=page_size))


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
