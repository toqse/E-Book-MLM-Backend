from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Case, Count, IntegerField, Q, When
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from apps.common.permissions import IsAdminRole, require_kyc_verified_and_compliant
from apps.common.responses import envelope_response
from apps.admin_panel.utils import get_system_config
from apps.wallet.bands import BAND_EDGES
from apps.wallet.models import Wallet

from .audit_log import log_sponsor_audit
from .models import SponsorSlotAuditEvent, SponsorSlotBatch, SponsorSlotCode
from .services import SponsorSlotService


def _parse_positive_int(raw, default: int, *, min_v: int = 1, max_v: int = 200) -> int:
    try:
        v = int(str(raw).strip())
    except Exception:
        v = default
    v = max(min_v, v)
    v = min(max_v, v)
    return v


def _days_remaining(expires_at) -> int:
    now = timezone.now()
    if not expires_at:
        return 0
    if timezone.is_aware(expires_at) and timezone.is_aware(now):
        delta: timedelta = expires_at - now
    else:
        delta = expires_at - now.replace(tzinfo=None)
    return max(0, delta.days)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_slots(request):
    blocked = require_kyc_verified_and_compliant(request)
    if blocked is not None:
        return blocked
    batches = SponsorSlotBatch.objects.filter(issued_to=request.user).prefetch_related("codes")
    data = []
    for b in batches:
        data.append(
            {
                "batch_id": b.id,
                "band": b.band_number,
                "expires_at": b.expires_at.isoformat(),
                "codes": [
                    {
                        "code": c.code,
                        "status": c.status,
                        "expires_at": c.expires_at.isoformat(),
                        "unlock_at_total_earned": str(c.unlock_at_total_earned)
                        if c.unlock_at_total_earned is not None
                        else None,
                    }
                    for c in b.codes.all()
                ],
            }
        )
    return envelope_response({"batches": data})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def bundle(request):
    """
    UI-aligned sponsor slots payload:
    - summary counters (active/redeemed/expired)
    - active slots list for cards
    - paginated history list for table
    """
    blocked = require_kyc_verified_and_compliant(request)
    if blocked is not None:
        return blocked
    user = request.user
    now = timezone.now()
    cfg = get_system_config()

    # Counts (single aggregate query).
    st = SponsorSlotCode.Status
    int_out = IntegerField()
    base_qs = SponsorSlotCode.objects.filter(issued_to=user)
    counts = base_qs.aggregate(
        active_count=Count(
            Case(
                When(
                    Q(status__in=(st.ACTIVE, st.SHARED)) & Q(expires_at__gt=now),
                    then=1,
                ),
                output_field=int_out,
            )
        ),
        redeemed_count=Count(
            Case(
                When(status=st.REDEEMED, then=1),
                output_field=int_out,
            )
        ),
        expired_count=Count(
            Case(
                When(Q(status=st.EXPIRED) | (Q(expires_at__lte=now) & ~Q(status=st.REDEEMED)), then=1),
                output_field=int_out,
            )
        ),
    )

    active_qs = (
        base_qs.filter(status__in=(st.ACTIVE, st.SHARED), expires_at__gt=now)
        .order_by("expires_at", "-id")
        .only("code", "status", "shared_via", "created_at", "expires_at")
    )
    active_slots = [
        {
            "code": c.code,
            "status": c.status,
            "shared_via": c.shared_via,
            "issued_at": c.created_at.isoformat(),
            "expires_at": c.expires_at.isoformat(),
            "days_remaining": _days_remaining(c.expires_at),
        }
        for c in active_qs
    ]

    page = _parse_positive_int(request.query_params.get("history_page"), 1, min_v=1, max_v=10000)
    page_size = _parse_positive_int(
        request.query_params.get("history_page_size"), 10, min_v=1, max_v=100
    )
    offset = (page - 1) * page_size
    hist_qs = (
        base_qs.select_related("redeemed_by")
        .order_by("-created_at", "-id")
        .only(
            "code",
            "status",
            "created_at",
            "expires_at",
            "redeemed_by__full_name",
            "redeemed_by__member_id",
        )
    )
    total = hist_qs.count()
    rows = list(hist_qs[offset : offset + page_size])
    commission_amount = str(cfg.direct_commission)
    history_results = []
    for c in rows:
        redeemed_by = None
        if c.redeemed_by_id:
            redeemed_by = {
                "full_name": c.redeemed_by.full_name,
                "member_id": c.redeemed_by.member_id,
            }
        history_results.append(
            {
                "code": c.code,
                "issued_at": c.created_at.isoformat(),
                "expires_at": c.expires_at.isoformat(),
                "status": c.status,
                "redeemed_by": redeemed_by,
                "commission_amount": commission_amount if c.status == st.REDEEMED else None,
            }
        )

    return envelope_response(
        {
            "summary": {
                "active_count": int(counts.get("active_count") or 0),
                "redeemed_count": int(counts.get("redeemed_count") or 0),
                "expired_count": int(counts.get("expired_count") or 0),
                "slot_expiry_days": int(getattr(cfg, "sponsor_slot_expiry_days", 0) or 0),
                "redeem_commission_amount": commission_amount,
            },
            "active_slots": active_slots,
            "history": {
                "count": int(total),
                "page": int(page),
                "page_size": int(page_size),
                "results": history_results,
            },
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def share_code(request, code: str):
    blocked = require_kyc_verified_and_compliant(request)
    if blocked is not None:
        return blocked
    c = SponsorSlotCode.objects.filter(code__iexact=code, issued_to=request.user).first()
    if not c:
        return envelope_response(None, message="Not found", success=False, status=404)
    if c.status in (
        SponsorSlotCode.Status.REDEEMED,
        SponsorSlotCode.Status.EXPIRED,
        SponsorSlotCode.Status.LOCKED,
    ) or timezone.now() > c.expires_at:
        return envelope_response(
            None,
            message="Code is not shareable",
            success=False,
            status=400,
            errors={"detail": "not_shareable"},
        )
    c.shared_via = request.data.get("channel", "COPY")
    c.status = SponsorSlotCode.Status.SHARED
    c.save(update_fields=["shared_via", "status"])
    return envelope_response({"ok": True})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def validate_public(request):
    """
    Sponsor slot validation + optional amount preview.

    Body:
      - sponsor_code | code (required)
      - ebook_id | ebook_slug (optional). If present -> single-ebook pricing preview.
      - if ebook_id/ebook_slug omitted -> cart pricing preview for current user.

    Notes:
      - Preview-only: does not reserve or redeem the code.
      - Checkout/create-order will re-validate.
    """
    from decimal import Decimal, InvalidOperation

    code = request.data.get("sponsor_code") or request.data.get("code")
    raw = (code or "").strip()
    if not raw:
        return envelope_response(
            None,
            message="Sponsor code is required",
            success=False,
            status=400,
            errors={"detail": "missing_code"},
        )

    slot = SponsorSlotService.validate_code(raw, redeemer=request.user)
    valid = bool(slot)
    issuer = slot.issued_to.full_name if slot else None

    ebook_id = request.data.get("ebook_id")
    ebook_slug = request.data.get("ebook_slug")

    totals_payload = None

    def _d(raw_s: str) -> Decimal:
        try:
            return Decimal(str(raw_s)).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError, ValueError):
            return Decimal("0.00")

    if ebook_id not in (None, "") or (ebook_slug or "").strip():
        # Single ebook preview (mirrors payments/services.py create_checkout_order math).
        from apps.courses.models import EBook

        ebook = None
        if ebook_id not in (None, ""):
            ebook = EBook.objects.filter(
                pk=ebook_id,
                status=EBook.Status.PUBLISHED,
            ).first()
        elif ebook_slug:
            ebook = EBook.objects.filter(
                slug=str(ebook_slug).strip(),
                status=EBook.Status.PUBLISHED,
            ).first()
        if not ebook:
            return envelope_response(
                None,
                message="Book not found or not published",
                success=False,
                status=404,
            )
        cfg = get_system_config()
        base = _d(ebook.price)
        gst_rate = _d(cfg.gst_rate)
        gst = (base * gst_rate).quantize(Decimal("0.01"))
        gateway = Decimal("5.72").quantize(Decimal("0.01"))
        total = (base + gst + gateway).quantize(Decimal("0.01"))
        # Sponsor slot discounts ONE ebook purchase.
        # Single-ebook preview includes gateway too.
        discount = total if valid else Decimal("0.00")
        net = (total - discount).quantize(Decimal("0.01"))
        totals_payload = {
            "flow": "single",
            "taxable_base": str(base),
            "gst_amount": str(gst),
            "gateway_charge": str(gateway),
            "total": str(total),
            "discount_amount": str(discount),
            "net_payable": str(net),
            "amount_paise": int((net * Decimal("100")).to_integral_value()),
        }
    else:
        # Cart preview (mirrors cart/services.py preview_checkout_totals math).
        from apps.cart.models import Cart, CartItem
        from apps.cart.services import preview_checkout_totals

        cart = Cart.objects.filter(user=request.user).first()
        if not cart:
            return envelope_response(
                None,
                message="Cart is empty",
                success=False,
                status=400,
                errors={"detail": "cart_empty"},
            )
        items = list(
            CartItem.objects.filter(cart=cart)
            .select_related("ebook")
            .order_by("ebook_id", "id")
        )
        if not items:
            return envelope_response(
                None,
                message="Cart is empty",
                success=False,
                status=400,
                errors={"detail": "cart_empty"},
            )
        ebooks = [it.ebook for it in items]
        # Mirror payments/services.py cart normalization: unique + sort by pk.
        ebooks = list({eb.pk: eb for eb in ebooks}.values())
        ebooks.sort(key=lambda e: e.pk)
        t = preview_checkout_totals(ebooks)
        total = _d(t.get("total"))
        # Sponsor slot discounts ONE ebook purchase in the cart (no gateway discount).
        cfg = get_system_config()
        gst_rate = _d(cfg.gst_rate)
        first_ebook_base = _d(ebooks[0].price)
        first_ebook_gst = (first_ebook_base * gst_rate).quantize(Decimal("0.01"))
        unit_discount = (first_ebook_base + first_ebook_gst).quantize(Decimal("0.01"))
        if len(ebooks) == 1:
            # If only one ebook in cart, gateway is also discounted (matches checkout behavior).
            unit_discount = (unit_discount + _d(t.get("gateway_charge"))).quantize(Decimal("0.01"))
        discount = min(total, unit_discount) if valid else Decimal("0.00")
        net = (total - discount).quantize(Decimal("0.01"))
        totals_payload = {
            "flow": "cart",
            "taxable_base": str(_d(t.get("taxable_base"))),
            "gst_amount": str(_d(t.get("gst_amount"))),
            "gateway_charge": str(_d(t.get("gateway_charge"))),
            "total": str(total),
            "discount_amount": str(discount),
            "net_payable": str(net),
            "amount_paise": int((net * Decimal("100")).to_integral_value()),
        }

    if not valid:
        return envelope_response(
            {"valid": False, "issuer": None, "totals": totals_payload},
            message="Invalid or expired",
            success=False,
            status=400,
        )

    return envelope_response({"valid": True, "issuer": issuer, "totals": totals_payload})


def _admin_days_remaining_for_row(c: SponsorSlotCode) -> int | None:
    if c.status in (SponsorSlotCode.Status.REDEEMED, SponsorSlotCode.Status.EXPIRED):
        return None
    return _days_remaining(c.expires_at)


@api_view(["GET"])
@permission_classes([IsAdminRole])
def admin_slots(request):
    page = _parse_positive_int(request.query_params.get("page"), 1, min_v=1, max_v=1_000_000)
    page_size = _parse_positive_int(
        request.query_params.get("page_size"), 20, min_v=1, max_v=100
    )
    q = (request.query_params.get("q") or "").strip()
    raw_status = (request.query_params.get("status") or "").strip().upper()

    base = SponsorSlotCode.objects.select_related("issued_to", "redeemed_by", "batch").order_by(
        "-created_at", "-id"
    )
    if q:
        base = base.filter(
            Q(code__icontains=q)
            | Q(issued_to__member_id__icontains=q)
            | Q(issued_to__full_name__icontains=q)
        )
    if raw_status and raw_status != "ALL":
        valid = {x for x, _ in SponsorSlotCode.Status.choices}
        if raw_status not in valid:
            return envelope_response(
                None,
                message="Invalid status filter.",
                success=False,
                errors={"detail": "invalid_status"},
                status=400,
            )
        base = base.filter(status=raw_status)

    total_count = base.count()
    total_pages = (total_count + page_size - 1) // page_size if total_count else 0
    start = (page - 1) * page_size
    rows = list(base[start : start + page_size])

    results = []
    for c in rows:
        redeemed_by = None
        if c.redeemed_by_id:
            redeemed_by = {
                "member_id": c.redeemed_by.member_id,
                "full_name": c.redeemed_by.full_name,
            }
        results.append(
            {
                "code": c.code,
                "issuer_name": c.issued_to.full_name,
                "issuer_member_id": c.issued_to.member_id,
                "issued_at": c.created_at.isoformat(),
                "expires_at": c.expires_at.isoformat(),
                "days_remaining": _admin_days_remaining_for_row(c),
                "redeemed_by": redeemed_by,
                "status": c.status,
                "is_flagged": c.is_flagged,
                "band_number": c.batch.band_number,
                "batch_id": c.batch_id,
            }
        )

    return envelope_response(
        {
            "results": results,
            "count": total_count,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }
    )


@api_view(["GET"])
@permission_classes([IsAdminRole])
def admin_slots_flagged(request):
    qs = (
        SponsorSlotCode.objects.filter(is_flagged=True)
        .select_related("issued_to")
        .order_by("-created_at", "-id")
    )
    return envelope_response(
        {
            "results": [
                {
                    "code": c.code,
                    "issuer_member_id": c.issued_to.member_id,
                    "status": c.status,
                }
                for c in qs
            ]
        }
    )


@api_view(["GET"])
@permission_classes([IsAdminRole])
def admin_slot_detail(request, code: str):
    c = (
        SponsorSlotCode.objects.filter(code__iexact=code.strip())
        .select_related("issued_to", "redeemed_by", "batch", "redeemed_order", "redeemed_order__ebook")
        .first()
    )
    if not c:
        return envelope_response(None, message="Not found", success=False, status=404)

    order_payload = None
    if c.redeemed_order_id:
        o = c.redeemed_order
        order_payload = {
            "id": o.id,
            "order_number": o.order_number,
            "status": o.status,
            "total_amount": str(o.total_amount),
            "paid_at": o.paid_at.isoformat() if o.paid_at else None,
            "ebook_id": o.ebook_id,
            "ebook_title": getattr(o.ebook, "title", None) if o.ebook_id else None,
        }

    redeemed_by = None
    if c.redeemed_by_id:
        redeemed_by = {
            "member_id": c.redeemed_by.member_id,
            "full_name": c.redeemed_by.full_name,
        }

    return envelope_response(
        {
            "code": c.code,
            "status": c.status,
            "is_flagged": c.is_flagged,
            "shared_via": c.shared_via,
            "unique_ips_attempted": c.unique_ips_attempted,
            "unlock_at_total_earned": str(c.unlock_at_total_earned)
            if c.unlock_at_total_earned is not None
            else None,
            "unlocked_at": c.unlocked_at.isoformat() if c.unlocked_at else None,
            "issued_at": c.created_at.isoformat(),
            "expires_at": c.expires_at.isoformat(),
            "issuer": {
                "member_id": c.issued_to.member_id,
                "full_name": c.issued_to.full_name,
            },
            "redeemed_by": redeemed_by,
            "redeemed_order": order_payload,
            "batch": {
                "id": c.batch.id,
                "band_number": c.batch.band_number,
                "total_codes": c.batch.total_codes,
                "codes_redeemed": c.batch.codes_redeemed,
                "codes_expired": c.batch.codes_expired,
                "expires_at": c.batch.expires_at.isoformat(),
            },
        }
    )


@api_view(["POST"])
@permission_classes([IsAdminRole])
def admin_issue_slot(request):
    member_id = (request.data.get("member_id") or "").strip()
    if not member_id:
        return envelope_response(
            None,
            message="member_id is required",
            success=False,
            status=400,
            errors={"detail": "missing_member_id"},
        )
    User = get_user_model()
    user = User.objects.filter(member_id__iexact=member_id).first()
    if not user:
        return envelope_response(None, message="Member not found", success=False, status=404)

    band_raw = request.data.get("band_number", 2)
    try:
        band_number = int(band_raw)
    except (TypeError, ValueError):
        return envelope_response(
            None,
            message="Invalid band_number",
            success=False,
            status=400,
            errors={"detail": "invalid_band_number"},
        )
    if band_number < 1 or band_number > len(BAND_EDGES):
        return envelope_response(
            None,
            message="band_number out of range",
            success=False,
            status=400,
            errors={"detail": "invalid_band_number"},
        )

    w = Wallet.objects.filter(user=user).first()
    earned = w.total_earned if w else Decimal("0")

    batch = SponsorSlotService.issue_batch(
        user,
        band_number,
        current_total_earned=earned,
    )
    codes = [
        {
            "code": co.code,
            "status": co.status,
            "expires_at": co.expires_at.isoformat(),
        }
        for co in batch.codes.order_by("id")
    ]

    return envelope_response(
        {
            "batch_id": batch.id,
            "band_number": batch.band_number,
            "expires_at": batch.expires_at.isoformat(),
            "codes": codes,
        }
    )


@api_view(["GET"])
@permission_classes([IsAdminRole])
def admin_audit_log(request):
    page = _parse_positive_int(request.query_params.get("page"), 1, min_v=1, max_v=1_000_000)
    page_size = _parse_positive_int(
        request.query_params.get("page_size"), 20, min_v=1, max_v=100
    )
    code_filter = (request.query_params.get("code") or "").strip()

    qs = SponsorSlotAuditEvent.objects.select_related("sponsor_slot_code", "actor").order_by(
        "-created_at", "-id"
    )
    if code_filter:
        qs = qs.filter(sponsor_slot_code__code__iexact=code_filter)

    total = qs.count()
    total_pages = (total + page_size - 1) // page_size if total else 0
    start = (page - 1) * page_size
    rows = list(qs[start : start + page_size])

    results = []
    for ev in rows:
        results.append(
            {
                "code": ev.sponsor_slot_code.code,
                "event_type": ev.event_type,
                "actor_member_id": ev.actor.member_id if ev.actor_id else None,
                "actor_full_name": ev.actor.full_name if ev.actor_id else None,
                "actor_is_system": ev.actor_id is None,
                "metadata": ev.metadata,
                "created_at": ev.created_at.isoformat(),
            }
        )

    return envelope_response(
        {
            "results": results,
            "count": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }
    )


@api_view(["POST"])
@permission_classes([IsAdminRole])
def admin_flag_code(request, code: str):
    c = SponsorSlotCode.objects.filter(code__iexact=code.strip()).first()
    if not c:
        return envelope_response(None, message="Not found", success=False, status=404)
    if not c.is_flagged:
        c.is_flagged = True
        c.save(update_fields=["is_flagged"])
        log_sponsor_audit(
            c,
            SponsorSlotAuditEvent.EventType.FLAGGED,
            actor=request.user,
            metadata={},
        )
    return envelope_response({"ok": True})


@api_view(["POST"])
@permission_classes([IsAdminRole])
def admin_clear_flag_code(request, code: str):
    c = SponsorSlotCode.objects.filter(code__iexact=code.strip()).first()
    if not c:
        return envelope_response(None, message="Not found", success=False, status=404)
    if c.is_flagged:
        c.is_flagged = False
        c.save(update_fields=["is_flagged"])
        log_sponsor_audit(
            c,
            SponsorSlotAuditEvent.EventType.AUDIT_CLEARED,
            actor=request.user,
            metadata={},
        )
    return envelope_response({"ok": True})


@api_view(["POST"])
@permission_classes([IsAdminRole])
def admin_expire_code(request, code: str):
    c = SponsorSlotCode.objects.filter(code__iexact=code.strip()).first()
    if not c:
        return envelope_response(None, message="Not found", success=False, status=404)
    prev = c.status
    if prev != SponsorSlotCode.Status.EXPIRED:
        c.status = SponsorSlotCode.Status.EXPIRED
        c.save(update_fields=["status"])
        log_sponsor_audit(
            c,
            SponsorSlotAuditEvent.EventType.EXPIRED,
            actor=request.user,
            metadata={"previous_status": prev},
        )
    return envelope_response({"ok": True})
