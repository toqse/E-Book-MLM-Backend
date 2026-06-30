import hashlib
import hmac
import logging
import secrets
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.admin_panel.utils import get_system_config
from apps.courses.models import EBook, Enrollment
from apps.mlm_tree.placement import open_placement_queue_if_needed
from apps.sponsor_slots.services import SponsorSlotService
from apps.users.models import User
from apps.users.services import maybe_activate_account_on_purchase

from apps.audit.services import write_audit
from apps.commissions.engine import CommissionEngine
from apps.finance.services.date_range import _indian_fy_bounds_for

from .models import CreditNote, GSTInvoice, Order, OrderLine, RefundRequest

logger = logging.getLogger(__name__)

Q2 = Decimal("0.01")


class RazorpayRefundError(Exception):
    """Raised when a Razorpay gateway refund cannot be completed (caller maps to HTTP)."""

    def __init__(self, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def normalize_billing_from_payload(data: dict | None) -> dict[str, str]:
    """Optional billing snapshot from create-order JSON. Values are trimmed and length-capped."""
    data = data or {}

    def clip(key: str, max_len: int) -> str:
        raw = data.get(key)
        if raw is None:
            return ""
        return str(raw).strip()[:max_len]

    return {
        "billing_line1": clip("billing_line1", 255),
        "billing_line2": clip("billing_line2", 255),
        "billing_city": clip("billing_city", 128),
        "billing_state": clip("billing_state", 128),
        "billing_postal_code": clip("billing_postal_code", 20),
        "billing_country": clip("billing_country", 128),
    }


def ensure_gst_invoice_pdf(order: Order, *, force: bool = False) -> None:
    inv = GSTInvoice.objects.filter(order_id=order.pk).first()
    if not inv:
        return
    # Storage backends / legacy rows can expose FileField.name as None.
    existing_name = (getattr(inv.pdf_file, "name", None) or "").strip()
    if existing_name and not force:
        return
    filename = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in inv.invoice_number) + ".pdf"
    try:
        order_full = (
            Order.objects.select_related("user", "ebook")
            .prefetch_related("lines", "lines__ebook")
            .get(pk=order.pk)
        )
        inv = GSTInvoice.objects.get(pk=inv.pk)
        if force and (getattr(inv.pdf_file, "name", None) or "").strip():
            inv.pdf_file.delete(save=False)
        from .invoice_pdf import build_invoice_pdf_bytes

        inv.pdf_file.save(
            filename,
            ContentFile(build_invoice_pdf_bytes(order_full, inv)),
            save=True,
        )
    except Exception:
        logger.exception("gst_invoice_pdf_failed order_id=%s", order.pk)


def _client():
    import razorpay  # noqa: WPS433 — defer import (SDK pulls pkg_resources at import time)

    cfg = get_system_config()
    key = (getattr(cfg, "razorpay_key_id", "") or "").strip() or settings.RAZORPAY_KEY_ID
    secret = (getattr(cfg, "razorpay_key_secret", "") or "").strip() or settings.RAZORPAY_KEY_SECRET
    if not key or not secret:
        return None
    return razorpay.Client(auth=(key, secret))


def get_razorpay_key_id() -> str:
    """Public Razorpay key_id for frontend checkout."""
    cfg = get_system_config()
    return (getattr(cfg, "razorpay_key_id", "") or "").strip() or settings.RAZORPAY_KEY_ID


def generate_order_number() -> str:
    d = timezone.localdate().strftime("%Y%m%d")
    return f"ORD-{d}-{secrets.token_hex(3).upper()}"


def user_has_enrollment_for_ebook(user: User, ebook: EBook) -> bool:
    return Enrollment.objects.filter(user=user, ebook=ebook).exists()


@transaction.atomic
def create_checkout_order(
    user: User,
    *,
    ebook: EBook | None = None,
    sponsor_code: str | None = None,
    is_retail: bool = False,
    billing: dict[str, str] | None = None,
):
    cfg = get_system_config()
    selected_ebook = ebook
    if selected_ebook is None:
        selected_ebook = EBook.objects.filter(
            is_primary=True,
            status=EBook.Status.PUBLISHED,
        ).first()
    if selected_ebook and selected_ebook.status != EBook.Status.PUBLISHED:
        raise RuntimeError("Selected book is not published")

    if ebook is not None and user_has_enrollment_for_ebook(user, selected_ebook):
        raise ValueError("You are already enrolled in this book")

    base = selected_ebook.price if selected_ebook else cfg.product_base_price
    gst_rate = Decimal(str(cfg.gst_rate))
    gst = (base * gst_rate).quantize(Decimal("0.01"))
    gateway = Decimal("5.72")
    total = (base + gst + gateway).quantize(Decimal("0.01"))

    discount = Decimal("0")
    sponsor_obj = None
    slot_code = None
    if sponsor_code:
        slot_code = SponsorSlotService.validate_code(sponsor_code.strip(), redeemer=user)
        if slot_code:
            # Sponsor slot discounts ONE ebook purchase.
            # Single-ebook checkout includes gateway too.
            discount = min(total, (base + gst + gateway).quantize(Decimal("0.01")))
            total = (total - discount).quantize(Decimal("0.01"))

    bill = billing or normalize_billing_from_payload(None)
    order = Order.objects.create(
        user=user,
        ebook=selected_ebook,
        order_number=generate_order_number(),
        base_price=base,
        gst_amount=gst,
        gateway_charge=gateway,
        total_amount=base + gst + gateway,
        discount_amount=discount,
        amount_paid=total,
        is_sponsor_slot_redemption=bool(slot_code),
        is_retail_purchase=is_retail,
        sponsor_code_used=slot_code,
        billing_line1=bill.get("billing_line1", ""),
        billing_line2=bill.get("billing_line2", ""),
        billing_city=bill.get("billing_city", ""),
        billing_state=bill.get("billing_state", ""),
        billing_postal_code=bill.get("billing_postal_code", ""),
        billing_country=bill.get("billing_country", ""),
    )

    if total == 0 and slot_code:
        finalize_zero_rupee_order(order, user, selected_ebook, slot_code)
        return order, None

    client = _client()
    if client is None:
        order.delete()
        raise RuntimeError("Razorpay not configured")

    rz_order = client.order.create(
        {
            "amount": int((order.amount_paid * Decimal("100")).to_integral_value()),
            "currency": "INR",
            "receipt": order.order_number,
        }
    )
    order.razorpay_order_id = rz_order["id"]
    order.save(update_fields=["razorpay_order_id"])
    return order, rz_order


@transaction.atomic
def create_checkout_order_from_cart(
    user: User,
    cart,
    *,
    sponsor_code: str | None = None,
    is_retail: bool = False,
    billing: dict[str, str] | None = None,
):
    """
    Build one Order with OrderLines from cart items, one Razorpay order for the total.
    Clears cart items only after order is persisted and Razorpay order is created (or zero-rupee finalize).
    """
    from apps.cart.models import Cart, CartItem

    cfg = get_system_config()
    cart_locked = Cart.objects.select_for_update().filter(pk=cart.pk, user_id=user.pk).first()
    if not cart_locked:
        raise ValueError("Cart not found")
    items = list(
        CartItem.objects.filter(cart=cart_locked).select_related("ebook").order_by("ebook_id", "id")
    )
    if not items:
        raise ValueError("Cart is empty")

    ebooks: list[EBook] = []
    for it in items:
        eb = it.ebook
        if eb.status != EBook.Status.PUBLISHED:
            raise ValueError("One or more books in the cart are not available")
        if user_has_enrollment_for_ebook(user, eb):
            raise ValueError("Your cart includes a book you are already enrolled in")
        ebooks.append(eb)

    ebooks = list({eb.pk: eb for eb in ebooks}.values())
    ebooks.sort(key=lambda e: e.pk)

    taxable_base = sum((Decimal(str(eb.price))).quantize(Decimal("0.01")) for eb in ebooks).quantize(
        Decimal("0.01")
    )
    gst_rate = Decimal(str(cfg.gst_rate))
    gst = (taxable_base * gst_rate).quantize(Decimal("0.01"))
    gateway = Decimal("5.72")
    total = (taxable_base + gst + gateway).quantize(Decimal("0.01"))

    discount = Decimal("0")
    slot_code = None
    if sponsor_code:
        slot_code = SponsorSlotService.validate_code(sponsor_code.strip(), redeemer=user)
        if slot_code:
            # Sponsor slot discounts ONE ebook purchase in the cart.
            # If the cart contains only ONE ebook, discount includes gateway too (net payable becomes 0).
            # If the cart contains multiple ebooks, gateway remains payable once per order.
            first_ebook_base = Decimal(str(ebooks[0].price)).quantize(Decimal("0.01"))
            first_ebook_gst = (first_ebook_base * gst_rate).quantize(Decimal("0.01"))
            unit_discount = (first_ebook_base + first_ebook_gst).quantize(Decimal("0.01"))
            if len(ebooks) == 1:
                unit_discount = (unit_discount + gateway).quantize(Decimal("0.01"))
            discount = min(total, unit_discount)
            total = (total - discount).quantize(Decimal("0.01"))

    bill = billing or normalize_billing_from_payload(None)
    first_ebook = ebooks[0]
    order = Order.objects.create(
        user=user,
        ebook=first_ebook,
        order_number=generate_order_number(),
        base_price=taxable_base,
        gst_amount=gst,
        gateway_charge=gateway,
        total_amount=taxable_base + gst + gateway,
        discount_amount=discount,
        amount_paid=total,
        is_sponsor_slot_redemption=bool(slot_code),
        is_retail_purchase=is_retail,
        sponsor_code_used=slot_code,
        billing_line1=bill.get("billing_line1", ""),
        billing_line2=bill.get("billing_line2", ""),
        billing_city=bill.get("billing_city", ""),
        billing_state=bill.get("billing_state", ""),
        billing_postal_code=bill.get("billing_postal_code", ""),
        billing_country=bill.get("billing_country", ""),
    )
    for eb in ebooks:
        OrderLine.objects.create(
            order=order,
            ebook=eb,
            unit_base_price=Decimal(str(eb.price)).quantize(Decimal("0.01")),
        )

    if total == 0 and slot_code:
        finalize_zero_rupee_order(order, user, None, slot_code)
        return order, None

    client = _client()
    if client is None:
        order.delete()
        raise RuntimeError("Razorpay not configured")

    try:
        rz_order = client.order.create(
            {
                "amount": int((order.amount_paid * Decimal("100")).to_integral_value()),
                "currency": "INR",
                "receipt": order.order_number,
            }
        )
    except Exception:
        order.delete()
        raise
    order.razorpay_order_id = rz_order["id"]
    order.save(update_fields=["razorpay_order_id"])
    return order, rz_order


def finalize_zero_rupee_order(order: Order, user: User, ebook: EBook | None, slot_code):
    order.status = Order.Status.PAID
    order.paid_at = timezone.now()
    order.amount_paid = Decimal("0")
    order.save()
    if slot_code:
        SponsorSlotService.redeem_on_order(slot_code, order, user)
    _grant_enrollment(order, user, ebook)
    _clear_cart_items_for_paid_order(order)
    _ensure_gst_invoice(order)
    _place_and_commission(order, user)


@transaction.atomic
def _grant_enrollment(order: Order, user: User, ebook: EBook | None):
    enrolled = False
    if order.lines.exists():
        for line in order.lines.select_related("ebook").order_by("id"):
            Enrollment.objects.get_or_create(
                user=user,
                ebook=line.ebook,
                order=order,
                defaults={"is_retail": order.is_retail_purchase},
            )
        enrolled = True
    else:
        eff = ebook if ebook is not None else order.ebook
        if eff is None:
            eff = EBook.objects.filter(
                is_primary=True,
                status=EBook.Status.PUBLISHED,
            ).first()
        if eff:
            Enrollment.objects.get_or_create(
                user=user,
                ebook=eff,
                order=order,
                defaults={"is_retail": order.is_retail_purchase},
            )
            enrolled = True

    if not enrolled:
        return

    user.is_member = True
    activated = maybe_activate_account_on_purchase(user)
    update_fields = ["is_member"]
    if activated:
        update_fields.append("account_status")
    user.save(update_fields=update_fields)


def _place_and_commission(order: Order, user: User):
    if order.is_retail_purchase:
        return
    open_placement_queue_if_needed(order, user)


def finalize_order_as_paid(order: Order, *, payment_id: str) -> Order:
    """
    Mark order PAID and run enrollment, placement queue, GST invoice — same as
    post-signature path in verify_payment. Idempotent if already PAID.
    Only allowed from CREATED (raises ValueError otherwise).
    """
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        if order.status == Order.Status.PAID:
            return order
        if order.status != Order.Status.CREATED:
            raise ValueError(
                f"Order cannot be finalized from status {order.status}; only CREATED is allowed."
            )
        rid = (payment_id or "").strip()[:64]
        order.status = Order.Status.PAID
        order.razorpay_payment_id = rid or None
        order.paid_at = timezone.now()
        cfg = get_system_config()
        order.refund_eligible_until = timezone.now() + timedelta(days=cfg.refund_window_days)
        order.save()
        _grant_enrollment(order, order.user, order.ebook)
        _clear_cart_items_for_paid_order(order)
        _place_and_commission(order, order.user)
        _ensure_gst_invoice(order)
        order_id = order.pk
        transaction.on_commit(lambda: _schedule_invoice_message(order_id))
    return order


def _schedule_invoice_message(order_id: int) -> None:
    from apps.notifications.tasks import send_invoice_message_task

    send_invoice_message_task.delay(order_id)


def _clear_cart_items_for_paid_order(order: Order) -> None:
    """
    Empty matching cart items only after successful payment.

    Cart checkout creates OrderLines; direct single-ebook orders may not.
    This is safe and idempotent (retries / double-verifies won't error).
    """
    from apps.cart.models import CartItem

    ebook_ids = list(order.lines.values_list("ebook_id", flat=True))
    if not ebook_ids and order.ebook_id:
        ebook_ids = [order.ebook_id]
    if not ebook_ids:
        return
    CartItem.objects.filter(cart__user_id=order.user_id, ebook_id__in=ebook_ids).delete()


def verify_payment(order: Order, payment_id: str, signature: str):
    if order.status == Order.Status.PAID:
        return order
    client = _client()
    if client is None:
        raise RuntimeError("Razorpay not configured")
    params = {
        "razorpay_order_id": order.razorpay_order_id,
        "razorpay_payment_id": payment_id,
        "razorpay_signature": signature,
    }
    client.utility.verify_payment_signature(params)
    return finalize_order_as_paid(order, payment_id=payment_id)


def resolve_gateway_payment_method(order: Order, payment_id_fallback: str | None = None) -> str:
    """Razorpay instrument (card, upi, netbanking, …) when fetch succeeds; else gateway/manual label."""
    pid = (order.razorpay_payment_id or payment_id_fallback or "").strip()
    if not pid:
        return "unknown"
    if pid.upper().startswith("MANUAL-"):
        return "manual"
    client = _client()
    if client is None:
        return "razorpay"
    try:
        p = client.payment.fetch(pid)
        m = (p.get("method") or "").strip().lower()
        return m if m else "razorpay"
    except Exception:
        logger.debug("resolve_gateway_payment_method fetch failed for %s", pid, exc_info=True)
        return "razorpay"


def _ensure_gst_invoice(order: Order):
    existing = GSTInvoice.objects.filter(order_id=order.pk).first()
    if existing is None:
        inv_num = f"INV-{timezone.now().year}-{order.id:05d}"
        base = order.base_price
        gst = order.gst_amount
        half = (gst / 2).quantize(Decimal("0.01"))
        GSTInvoice.objects.create(
            order=order,
            invoice_number=inv_num,
            base_amount=base,
            cgst=half,
            sgst=half,
            total_gst=gst,
            discount=order.discount_amount,
            grand_total=order.base_price + order.gst_amount,
        )
        order.gst_invoice_number = inv_num
        order.save(update_fields=["gst_invoice_number"])
    ensure_gst_invoice_pdf(order)


def verify_webhook_signature(body: bytes, signature: str) -> bool:
    cfg = get_system_config()
    secret_s = (getattr(cfg, "razorpay_key_secret", "") or "").strip() or settings.RAZORPAY_KEY_SECRET
    secret = (secret_s or "").encode()
    digest = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature or "")


def payment_method_for_refund(order: Order) -> str:
    rid = (order.razorpay_payment_id or "").strip()
    if rid.startswith("MANUAL-"):
        return RefundRequest.PaymentMethod.DIRECT
    return RefundRequest.PaymentMethod.GATEWAY


REFUND_SUM_TOLERANCE = Decimal("0.02")


def amount_paid_share_by_order_line_id(order: Order) -> dict[int, Decimal]:
    """Split ``order.amount_paid`` across lines by ``unit_base_price`` ratio; last line absorbs rounding."""
    lines = list(order.lines.order_by("id").only("id", "unit_base_price"))
    if not lines:
        return {}
    ap = order.amount_paid.quantize(Decimal("0.01"))
    tot = sum((ln.unit_base_price for ln in lines), Decimal("0")).quantize(Decimal("0.01"))
    out: dict[int, Decimal] = {}
    if tot <= 0:
        n = len(lines)
        acc = Decimal("0")
        for i, ln in enumerate(lines):
            if i == n - 1:
                out[ln.pk] = (ap - acc).quantize(Decimal("0.01"))
            else:
                share = (ap / n).quantize(Decimal("0.01"))
                out[ln.pk] = share
                acc += share
        return out
    acc = Decimal("0")
    for i, ln in enumerate(lines):
        if i == len(lines) - 1:
            out[ln.pk] = (ap - acc).quantize(Decimal("0.01"))
        else:
            share = (ap * (ln.unit_base_price / tot)).quantize(Decimal("0.01"))
            out[ln.pk] = share
            acc += share
    return out


def sum_approved_refund_amounts(order_id: int) -> Decimal:
    s = RefundRequest.objects.filter(
        order_id=order_id,
        status=RefundRequest.Status.APPROVED,
    ).aggregate(s=Sum("amount"))["s"]
    return (s or Decimal("0")).quantize(Decimal("0.01"))


def sum_approved_refund_amounts_for_line(order_line_id: int) -> Decimal:
    s = RefundRequest.objects.filter(
        order_line_id=order_line_id,
        status=RefundRequest.Status.APPROVED,
    ).aggregate(s=Sum("amount"))["s"]
    return (s or Decimal("0")).quantize(Decimal("0.01"))


def remaining_refundable_for_order(order: Order) -> Decimal:
    return (order.amount_paid - sum_approved_refund_amounts(order.pk)).quantize(Decimal("0.01"))


def remaining_refundable_for_order_line(order: Order, order_line_id: int) -> Decimal:
    shares = amount_paid_share_by_order_line_id(order)
    cap = shares.get(order_line_id, Decimal("0"))
    used = sum_approved_refund_amounts_for_line(order_line_id)
    return (cap - used).quantize(Decimal("0.01"))


def refund_submission_blocked(order_id: int, *, target_order_line_id: int | None) -> bool:
    open_q = RefundRequest.objects.filter(
        order_id=order_id,
        status__in=(
            RefundRequest.Status.PENDING,
            RefundRequest.Status.PROCESSING,
        ),
    )
    if open_q.filter(order_line__isnull=True).exists():
        return True
    if target_order_line_id is not None:
        return open_q.filter(order_line_id=target_order_line_id).exists()
    return open_q.exists()


def latest_applicable_refund_request(
    rlist: list[RefundRequest],
    *,
    item_line_id: int | None,
) -> RefundRequest | None:
    """
    Highest-id RefundRequest that applies to this catalog item: line-scoped rows for ``item_line_id``,
    plus order-wide rows (``order_line_id`` null). Legacy single-ebook synthetic item uses only order-wide rows.
    """
    if item_line_id is not None:
        applicable = [r for r in rlist if r.order_line_id == item_line_id or r.order_line_id is None]
    else:
        applicable = [r for r in rlist if r.order_line_id is None]
    if not applicable:
        return None
    return max(applicable, key=lambda r: r.pk)


def latest_applicable_refund_status(
    rlist: list[RefundRequest],
    *,
    item_line_id: int | None,
) -> str | None:
    r = latest_applicable_refund_request(rlist, item_line_id=item_line_id)
    return r.status if r else None


def _next_refund_reference_locked(order_pk: int) -> str:
    yr = timezone.now().year
    n = RefundRequest.objects.filter(order_id=order_pk).count() + 1
    return f"RET-{yr}-{order_pk}-{n:03d}"


def refund_razorpay_payment_for_order(
    order: Order,
    *,
    refund_reference: str,
    amount_inr: Decimal,
) -> str | None:
    """
    Create a Razorpay refund for a GATEWAY capture. Returns refund id or None when DIRECT/offline payment (skip).

    Raises RazorpayRefundError when refund was required but could not be created.
    """
    if payment_method_for_refund(order) == RefundRequest.PaymentMethod.DIRECT:
        return None

    pid = (order.razorpay_payment_id or "").strip()
    if not pid:
        raise RazorpayRefundError(
            "No Razorpay payment id on this order; cannot refund via gateway.",
            status_code=400,
        )

    client = _client()
    if client is None:
        raise RazorpayRefundError(
            "Razorpay is not configured.",
            status_code=503,
        )

    amt = amount_inr.quantize(Decimal("0.01"))
    paise = int((amt * Decimal(100)).quantize(Decimal("1")))
    if paise <= 0:
        raise RazorpayRefundError("Invalid refund amount.", status_code=400)

    ref_note = (refund_reference or "").strip()[:225]
    ord_note = (order.order_number or "").strip()[:225]
    payload = {
        "amount": paise,
        "notes": {"refund_reference": ref_note, "order_number": ord_note},
    }
    try:
        resp = client.payment.refund(pid, payload)
    except Exception as exc:
        logger.exception(
            "razorpay_refund_failed payment_id=%s order_id=%s",
            pid[:20],
            order.pk,
        )
        detail = getattr(exc, "description", None) or getattr(exc, "message", None)
        if isinstance(detail, dict):
            detail = detail.get("description") or str(detail)
        raise RazorpayRefundError(
            str(detail or exc)[:500],
            status_code=502,
        ) from exc

    refund_id = None
    if isinstance(resp, dict):
        refund_id = resp.get("id")
    if not refund_id:
        raise RazorpayRefundError(
            "Razorpay returned no refund id.",
            status_code=502,
        )
    return str(refund_id)


def _next_credit_note_number_locked() -> str:
    fy_start, _ = _indian_fy_bounds_for(timezone.localdate())
    yy1 = fy_start.year % 100
    yy2 = (fy_start.year + 1) % 100
    n = CreditNote.objects.filter(created_at__date__gte=fy_start).count() + 1
    return f"CN-FY{yy1:02d}{yy2:02d}-{n:05d}"


def _refund_proportion(amount: Decimal, cap: Decimal) -> Decimal:
    if cap <= Decimal("0"):
        return Decimal("1")
    return min((amount / cap).quantize(Decimal("0.0001")), Decimal("1"))


def _credit_note_amounts(order: Order, rr: RefundRequest) -> tuple[Decimal, Decimal]:
    """Return (base_amount, total_gst) for the credit note tied to this refund."""
    cfg = get_system_config()
    gst_rate = Decimal(str(cfg.gst_rate))

    if rr.order_line_id:
        line = rr.order_line
        if line is None:
            line = OrderLine.objects.get(pk=rr.order_line_id)
        line_base = line.unit_base_price.quantize(Q2)
        line_gst = (line_base * gst_rate).quantize(Q2)
        shares = amount_paid_share_by_order_line_id(order)
        line_share = shares.get(rr.order_line_id, Decimal("0"))
        proportion = _refund_proportion(rr.amount, line_share)
        return (
            (line_base * proportion).quantize(Q2),
            (line_gst * proportion).quantize(Q2),
        )

    paid = order.amount_paid.quantize(Q2)
    proportion = _refund_proportion(rr.amount, paid)
    return (
        (order.base_price * proportion).quantize(Q2),
        (order.gst_amount * proportion).quantize(Q2),
    )


def create_credit_note_for_refund(
    *,
    order: Order,
    rr: RefundRequest,
    actor=None,
) -> CreditNote | None:
    """
    Issue a credit note for an approved refund. Returns None when no GST was collected
    (zero amount_paid) or when no GST invoice exists (legacy orders).
    """
    existing = CreditNote.objects.filter(refund_request_id=rr.pk).first()
    if existing is not None:
        return existing

    paid = order.amount_paid.quantize(Q2)
    if paid <= Decimal("0"):
        logger.info(
            "credit_note skipped zero paid order_id=%s refund=%s",
            order.pk,
            rr.reference,
        )
        return None

    inv = GSTInvoice.objects.select_for_update().filter(order_id=order.pk).first()
    if inv is None:
        logger.warning(
            "credit_note skipped no invoice order_id=%s refund=%s",
            order.pk,
            rr.reference,
        )
        if actor is not None:
            write_audit(
                "credit_note.skipped_no_invoice",
                actor=actor,
                target_type="RefundRequest",
                target_id=str(rr.pk),
                payload={
                    "order_number": order.order_number,
                    "refund_reference": rr.reference,
                },
            )
        return None

    cn_base, cn_gst = _credit_note_amounts(order, rr)
    half = (cn_gst / 2).quantize(Q2)
    if rr.order_line_id:
        shares = amount_paid_share_by_order_line_id(order)
        cap = shares.get(rr.order_line_id, Decimal("0"))
    else:
        cap = paid
    proportion = _refund_proportion(rr.amount, cap)
    cn_discount = (order.discount_amount * proportion).quantize(Q2)

    cn = CreditNote.objects.create(
        gst_invoice=inv,
        refund_request=rr,
        credit_note_number=_next_credit_note_number_locked(),
        hsn_sac_code=inv.hsn_sac_code,
        base_amount=cn_base,
        cgst=half,
        sgst=half,
        total_gst=cn_gst,
        discount=cn_discount,
        grand_total=(cn_base + cn_gst).quantize(Q2),
        reason="refund",
    )
    write_audit(
        "credit_note.issued",
        actor=actor,
        target_type="CreditNote",
        target_id=str(cn.pk),
        payload={
            "order_number": order.order_number,
            "refund_reference": rr.reference,
            "credit_note_number": cn.credit_note_number,
            "total_gst": str(cn_gst),
        },
    )
    return cn


def apply_approved_refund_fulfillment(
    *,
    order: Order,
    rr: RefundRequest,
    actor,
    razorpay_refund_id: str | None,
) -> None:
    """
    After gateway refund succeeds: remove access (enrollments), then if cumulative approved
    refunds cover ``order.amount_paid``, mark order REFUNDED and reverse commissions.
    """
    if order.status != Order.Status.PAID:
        raise ValueError(f"Order must be PAID to fulfill refund (got {order.status})")

    if rr.order_line_id:
        line = rr.order_line
        Enrollment.objects.filter(
            order_id=order.pk,
            user_id=order.user_id,
            ebook_id=line.ebook_id,
        ).delete()
    else:
        Enrollment.objects.filter(order_id=order.pk).delete()

    paid = order.amount_paid.quantize(Decimal("0.01"))
    prev = sum_approved_refund_amounts(order.pk)
    total = (prev + rr.amount).quantize(Decimal("0.01"))
    if total + REFUND_SUM_TOLERANCE >= paid:
        order.status = Order.Status.REFUNDED
        order.save(update_fields=["status"])
        CommissionEngine.reverse_commissions(order)
        Enrollment.objects.filter(order_id=order.pk).delete()

    cn = create_credit_note_for_refund(order=order, rr=rr, actor=actor)
    if paid > Decimal("0") and GSTInvoice.objects.filter(order_id=order.pk).exists() and cn is None:
        raise RuntimeError(
            f"Credit note required for refund {rr.reference} on order {order.order_number}"
        )

    audit_payload: dict = {"order_number": order.order_number, "refund_reference": rr.reference}
    if razorpay_refund_id:
        audit_payload["razorpay_refund_id"] = razorpay_refund_id
    write_audit(
        "refund.approved_fulfilled",
        actor=actor,
        target_type="Order",
        target_id=str(order.pk),
        payload=audit_payload,
    )


def submit_member_refund_request(
    *,
    user,
    order_id: int,
    member_note: str = "",
    order_line_id: int | None = None,
) -> RefundRequest:
    """Create a pending refund request. Raises ValueError for business-rule violations."""
    now = timezone.now()
    note = (member_note or "").strip()
    if len(note) > 4000:
        raise ValueError("Note too long")

    with transaction.atomic():
        qs = Order.objects.select_for_update().filter(pk=order_id, user=user)
        o = qs.first()
        if not o:
            raise ValueError("Order not found")
        if o.status != Order.Status.PAID:
            raise ValueError("Not refundable")
        if o.refund_eligible_until and now > o.refund_eligible_until:
            raise ValueError("Refund window closed")

        target_line: OrderLine | None = None
        rlist = list(RefundRequest.objects.filter(order_id=o.pk))

        if order_line_id is not None:
            if not o.lines.exists():
                raise ValueError("Line-based refund is not available for this order")
            target_line = (
                OrderLine.objects.select_for_update()
                .filter(pk=int(order_line_id), order_id=o.pk)
                .first()
            )
            if not target_line:
                raise ValueError("Order line not found")
            if latest_applicable_refund_status(rlist, item_line_id=target_line.pk) == RefundRequest.Status.REJECTED:
                raise ValueError(
                    "Refund was already rejected for this item; resubmission is not allowed."
                )
            if refund_submission_blocked(o.pk, target_order_line_id=target_line.pk):
                raise ValueError("Refund request already pending")
            amount = remaining_refundable_for_order_line(o, target_line.pk)
        else:
            if latest_applicable_refund_status(rlist, item_line_id=None) == RefundRequest.Status.REJECTED:
                raise ValueError(
                    "Refund was already rejected for this item; resubmission is not allowed."
                )
            if refund_submission_blocked(o.pk, target_order_line_id=None):
                raise ValueError("Refund request already pending")
            amount = remaining_refundable_for_order(o)

        if amount <= Decimal("0"):
            raise ValueError("Nothing left to refund for this selection")

        ref = _next_refund_reference_locked(o.pk)
        return RefundRequest.objects.create(
            reference=ref,
            order=o,
            user=o.user,
            order_line=target_line,
            amount=amount,
            payment_method=payment_method_for_refund(o),
            status=RefundRequest.Status.PENDING,
            member_note=note,
        )


def submit_member_refund_requests_for_lines(
    *,
    user,
    order_id: int,
    order_line_ids: list[int],
    member_note: str = "",
) -> list[RefundRequest]:
    """
    Create one pending RefundRequest per distinct order line id, in a single transaction.
    Same business rules as single-line submit per line; fails entirely if any line is invalid or blocked.
    """
    now = timezone.now()
    note = (member_note or "").strip()
    if len(note) > 4000:
        raise ValueError("Note too long")

    if not order_line_ids:
        raise ValueError("order_line_ids must be a non-empty list")
    try:
        raw_ids = [int(x) for x in order_line_ids]
    except (TypeError, ValueError):
        raise ValueError("order_line_ids must be a list of integers")
    deduped = list(dict.fromkeys(raw_ids))
    if not deduped:
        raise ValueError("order_line_ids must be a non-empty list")

    created: list[RefundRequest] = []
    with transaction.atomic():
        qs = Order.objects.select_for_update().filter(pk=order_id, user=user)
        o = qs.first()
        if not o:
            raise ValueError("Order not found")
        if o.status != Order.Status.PAID:
            raise ValueError("Not refundable")
        if o.refund_eligible_until and now > o.refund_eligible_until:
            raise ValueError("Refund window closed")
        if not o.lines.exists():
            raise ValueError("order_line_ids is not available for this order")

        if RefundRequest.objects.filter(
            order_id=o.pk,
            order_line__isnull=True,
            status__in=(
                RefundRequest.Status.PENDING,
                RefundRequest.Status.PROCESSING,
            ),
        ).exists():
            raise ValueError("Refund request already pending")

        rlist = list(RefundRequest.objects.filter(order_id=o.pk))

        lines_to_create: list[tuple[OrderLine, Decimal]] = []
        for lid in deduped:
            line = (
                OrderLine.objects.select_for_update()
                .filter(pk=int(lid), order_id=o.pk)
                .first()
            )
            if not line:
                raise ValueError("Order line not found")
            if latest_applicable_refund_status(rlist, item_line_id=line.pk) == RefundRequest.Status.REJECTED:
                raise ValueError(
                    "Refund was already rejected for this item; resubmission is not allowed."
                )
            if refund_submission_blocked(o.pk, target_order_line_id=line.pk):
                raise ValueError("Refund request already pending")
            amount = remaining_refundable_for_order_line(o, line.pk)
            if amount <= Decimal("0"):
                raise ValueError("Nothing left to refund for this selection")
            lines_to_create.append((line, amount))

        pm = payment_method_for_refund(o)
        for line, amount in lines_to_create:
            ref = _next_refund_reference_locked(o.pk)
            created.append(
                RefundRequest.objects.create(
                    reference=ref,
                    order=o,
                    user=o.user,
                    order_line=line,
                    amount=amount,
                    payment_method=pm,
                    status=RefundRequest.Status.PENDING,
                    member_note=note,
                )
            )
    return created
