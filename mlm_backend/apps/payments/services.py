import hashlib
import hmac
import secrets
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.admin_panel.utils import get_system_config
from apps.courses.models import EBook, Enrollment
from apps.mlm_tree.placement import open_placement_queue_if_needed
from apps.sponsor_slots.services import SponsorSlotService
from apps.users.models import User

from .models import GSTInvoice, Order


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


@transaction.atomic
def create_checkout_order(
    user: User,
    *,
    ebook: EBook | None = None,
    sponsor_code: str | None = None,
    is_retail: bool = False,
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
            discount = total
            total = Decimal("0")

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


def finalize_zero_rupee_order(order: Order, user: User, ebook: EBook | None, slot_code):
    order.status = Order.Status.PAID
    order.paid_at = timezone.now()
    order.amount_paid = Decimal("0")
    order.save()
    if slot_code:
        SponsorSlotService.redeem_on_order(slot_code, order, user)
    _grant_enrollment(order, user, ebook)
    _ensure_gst_invoice(order)
    _place_and_commission(order, user)


@transaction.atomic
def _grant_enrollment(order: Order, user: User, ebook: EBook | None):
    if ebook is None:
        ebook = order.ebook
    if ebook is None:
        ebook = EBook.objects.filter(
            is_primary=True,
            status=EBook.Status.PUBLISHED,
        ).first()
    if not ebook:
        return
    Enrollment.objects.get_or_create(
        user=user,
        ebook=ebook,
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
        ebook = order.ebook or EBook.objects.filter(
            is_primary=True,
            status=EBook.Status.PUBLISHED,
        ).first()
        _grant_enrollment(order, order.user, ebook)
        _place_and_commission(order, order.user)
        _ensure_gst_invoice(order)
    return order


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
    if hasattr(order, "gst_invoice"):
        return
    cfg = get_system_config()
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


def verify_webhook_signature(body: bytes, signature: str) -> bool:
    secret = settings.RAZORPAY_KEY_SECRET.encode()
    digest = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature or "")
