import json
import secrets

from django.conf import settings
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated

from apps.audit.services import write_audit
from apps.commissions.engine import CommissionEngine
from apps.common.permissions import IsFinanceAdmin, IsSuperAdmin
from apps.common.responses import envelope_response
from apps.courses.models import EBook, Enrollment

from .models import Order
from .services import (
    create_checkout_order,
    finalize_order_as_paid,
    verify_payment,
    verify_webhook_signature,
)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_order(request):
    sponsor_code = request.data.get("sponsor_code") or request.data.get("sponsor_slot_code")
    is_retail = bool(request.data.get("is_retail", False))
    ebook_id = request.data.get("ebook_id")
    slug = request.data.get("ebook_slug")

    ebook = None
    if ebook_id not in (None, ""):
        ebook = EBook.objects.filter(pk=ebook_id, status=EBook.Status.PUBLISHED).first()
    elif slug:
        ebook = EBook.objects.filter(slug=slug, status=EBook.Status.PUBLISHED).first()

    if (ebook_id not in (None, "") or slug) and not ebook:
        return envelope_response(
            None,
            message="Book not found or not published",
            success=False,
            status=404,
        )
    try:
        order, rz = create_checkout_order(
            request.user,
            ebook=ebook,
            sponsor_code=sponsor_code,
            is_retail=is_retail,
        )
    except RuntimeError as e:
        return envelope_response(None, message=str(e), success=False, status=500)
    except Exception as e:
        # External gateway/library errors (e.g. bad Razorpay credentials or network issues)
        # should still return API envelope JSON instead of a raw Django 500 HTML page.
        return envelope_response(None, message=str(e), success=False, status=500)
    if rz is None:
        return envelope_response(
            {
                "order_id": order.id,
                "order_number": order.order_number,
                "amount_paise": 0,
                "razorpay_order_id": None,
                "key_id": settings.RAZORPAY_KEY_ID,
                "status": order.status,
            }
        )
    return envelope_response(
        {
            "order_id": order.id,
            "order_number": order.order_number,
            "amount_paise": rz["amount"],
            "razorpay_order_id": rz["id"],
            "key_id": settings.RAZORPAY_KEY_ID,
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def verify(request):
    order_id = request.data.get("order_id")
    payment_id = request.data.get("razorpay_payment_id")
    signature = request.data.get("razorpay_signature")
    order = Order.objects.filter(pk=order_id, user=request.user).first()
    if not order:
        return envelope_response(None, message="Order not found", success=False, status=404)
    try:
        verify_payment(order, payment_id, signature)
    except Exception as exc:
        return envelope_response(None, message=str(exc), success=False, status=400)
    return envelope_response({"status": "PAID", "order_number": order.order_number})


@api_view(["POST"])
@permission_classes([AllowAny])
def webhook(request):
    body = request.body
    sig = request.headers.get("X-Razorpay-Signature", "")
    if not verify_webhook_signature(body, sig):
        return envelope_response(None, message="Bad signature", success=False, status=400)
    payload = json.loads(body.decode("utf-8"))
    return envelope_response({"received": True, "event": payload.get("event")})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_orders(request):
    qs = Order.objects.filter(user=request.user).order_by("-id")[:50]
    data = [
        {
            "id": o.id,
            "order_number": o.order_number,
            "status": o.status,
            "amount_paid": str(o.amount_paid),
        }
        for o in qs
    ]
    return envelope_response({"results": data})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def order_invoice(request, pk: int):
    o = Order.objects.filter(pk=pk, user=request.user).first()
    if not o or not hasattr(o, "gst_invoice"):
        return envelope_response(None, message="No invoice", success=False, status=404)
    inv = o.gst_invoice
    return envelope_response({"invoice_number": inv.invoice_number, "pdf_url": inv.pdf_url})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def order_refund(request, pk: int):
    o = Order.objects.filter(pk=pk, user=request.user).first()
    if not o or o.status != Order.Status.PAID:
        return envelope_response(None, message="Not refundable", success=False, status=400)
    if o.refund_eligible_until and timezone.now() > o.refund_eligible_until:
        return envelope_response(None, message="Refund window closed", success=False, status=400)
    o.status = Order.Status.REFUNDED
    o.save(update_fields=["status"])
    CommissionEngine.reverse_commissions(o)
    Enrollment.objects.filter(order=o).delete()
    return envelope_response({"status": "REFUNDED"})


def _resolve_order_for_admin_verify(order_ref: str) -> Order | None:
    """Numeric `order_ref` = primary key; otherwise match `order_number` (e.g. ORD-20260504-ABC)."""
    ref = (order_ref or "").strip()
    if not ref:
        return None
    if ref.isdigit():
        return Order.objects.filter(pk=int(ref)).first()
    return Order.objects.filter(order_number__iexact=ref).first()


@api_view(["POST"])
@permission_classes([IsSuperAdmin])
def admin_verify_payment_manual(request, order_ref: str):
    """Mark a CREATED order as PAID with same side effects as Razorpay verify (dev / ops)."""
    order = _resolve_order_for_admin_verify(order_ref)
    if not order:
        return envelope_response(None, message="Order not found", success=False, status=404)
    if order.status == Order.Status.PAID:
        return envelope_response(
            {
                "status": "PAID",
                "order_number": order.order_number,
                "order_id": order.id,
            },
            message="Order was already paid",
        )
    note = (request.data.get("note") or "").strip()
    payment_id = f"MANUAL-{request.user.pk}-{secrets.token_hex(8)}"
    if len(payment_id) > 64:
        payment_id = payment_id[:64]
    try:
        finalize_order_as_paid(order, payment_id=payment_id)
    except ValueError as exc:
        return envelope_response(None, message=str(exc), success=False, status=400)
    order.refresh_from_db()
    write_audit(
        "payment.admin_verified",
        actor=request.user,
        target_type="Order",
        target_id=str(order.id),
        payload={
            "order_number": order.order_number,
            "buyer_id": order.user_id,
            "payment_id": payment_id,
            "note": note or None,
        },
    )
    return envelope_response(
        {
            "status": "PAID",
            "order_number": order.order_number,
            "order_id": order.id,
        },
        message="Order marked paid",
    )


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def admin_orders(request):
    qs = Order.objects.all().order_by("-id")[:200]
    return envelope_response({"results": [{"id": x.id, "status": x.status} for x in qs]})


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def admin_revenue(request):
    return envelope_response({"today": "0", "week": "0", "month": "0"})


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def admin_gst_report(request):
    return envelope_response({"gstr1": []})
