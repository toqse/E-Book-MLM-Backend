import hashlib
import hmac
import logging
import secrets
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from apps.admin_panel.utils import get_system_config
from apps.courses.models import EBook, Enrollment
from apps.mlm_tree.placement import open_placement_queue_if_needed
from apps.sponsor_slots.services import SponsorSlotService
from apps.users.models import User

from .models import GSTInvoice, Order, OrderLine

logger = logging.getLogger(__name__)


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

    key = settings.RAZORPAY_KEY_ID
    secret = settings.RAZORPAY_KEY_SECRET
    if not key or not secret:
        return None
    return razorpay.Client(auth=(key, secret))


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
    if order.lines.exists():
        for line in order.lines.select_related("ebook").order_by("id"):
            Enrollment.objects.get_or_create(
                user=user,
                ebook=line.ebook,
                order=order,
                defaults={"is_retail": order.is_retail_purchase},
            )
        user.is_member = True
        user.save(update_fields=["is_member"])
        return

    eff = ebook if ebook is not None else order.ebook
    if eff is None:
        eff = EBook.objects.filter(
            is_primary=True,
            status=EBook.Status.PUBLISHED,
        ).first()
    if not eff:
        return
    Enrollment.objects.get_or_create(
        user=user,
        ebook=eff,
        order=order,
        defaults={"is_retail": order.is_retail_purchase},
    )
    user.is_member = True
    user.save(update_fields=["is_member"])


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
    return order


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
    secret = settings.RAZORPAY_KEY_SECRET.encode()
    digest = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature or "")
