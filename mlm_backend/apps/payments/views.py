import json
import logging
import secrets

from django.conf import settings
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.http import FileResponse
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated

from apps.audit.services import write_audit
from apps.commissions.engine import CommissionEngine
from apps.common.permissions import IsFinanceAdmin, IsSuperAdmin
from apps.common.responses import envelope_response
from apps.courses.models import EBook, Enrollment

from .models import GSTInvoice, Order
from .services import (
    create_checkout_order,
    ensure_gst_invoice_pdf,
    finalize_order_as_paid,
    normalize_billing_from_payload,
    verify_payment,
    verify_webhook_signature,
)

logger = logging.getLogger(__name__)
_invoice_link_signer = TimestampSigner(salt="payments.invoice.download")
_INVOICE_LINK_MAX_AGE_SECONDS = 60 * 60 * 24  # 24h


def _invoice_pdf_url(request, inv: GSTInvoice):
    """Return an absolute URL when possible; never raises (storage / Host header issues)."""
    legacy = (inv.pdf_url or "").strip() or None

    names = getattr(inv.pdf_file, "name", "") or ""
    if not names.strip():
        return legacy

    try:
        rel_url = inv.pdf_file.url
    except Exception:
        logger.exception("invoice_pdf_file_url_failed invoice_id=%s", inv.pk)
        return legacy

    if rel_url.startswith(("http://", "https://")):
        return rel_url

    try:
        return request.build_absolute_uri(rel_url)
    except Exception:
        logger.exception(
            "invoice_build_absolute_uri_failed invoice_id=%s rel=%s",
            inv.pk,
            rel_url,
        )
        return rel_url if rel_url else legacy


def _ebook_thumbnail_url(request, ebook: EBook | None):
    if not ebook:
        return None
    names = getattr(ebook.thumbnail, "name", "") or ""
    if not names.strip():
        return None
    try:
        rel_url = ebook.thumbnail.url
    except Exception:
        logger.exception("ebook_thumbnail_url_failed ebook_id=%s", ebook.pk)
        return None
    if rel_url.startswith(("http://", "https://")):
        return rel_url
    try:
        return request.build_absolute_uri(rel_url)
    except Exception:
        logger.exception(
            "ebook_thumbnail_build_absolute_uri_failed ebook_id=%s rel=%s",
            ebook.pk,
            rel_url,
        )
        return rel_url or None


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
    billing = normalize_billing_from_payload(request.data)
    try:
        order, rz = create_checkout_order(
            request.user,
            ebook=ebook,
            sponsor_code=sponsor_code,
            is_retail=is_retail,
            billing=billing,
        )
    except ValueError as e:
        return envelope_response(None, message=str(e), success=False, status=400)
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
    qs = (
        Order.objects.filter(user=request.user, status=Order.Status.PAID)
        .select_related("ebook")
        .order_by("-id")[:50]
    )
    data = []
    for o in qs:
        purchased_at = o.paid_at or o.created_at
        row = {
            "id": o.id,
            "order_number": o.order_number,
            "status": o.status,
            "purchased_at": purchased_at.isoformat() if purchased_at else None,
            "ebook_title": o.ebook.title if o.ebook else None,
            "thumbnail_url": _ebook_thumbnail_url(request, o.ebook),
            "amount_paid": str(o.amount_paid),
            "invoice_url": None,
        }
        try:
            inv = GSTInvoice.objects.filter(order_id=o.id).first()
            if inv:
                ensure_gst_invoice_pdf(o)
                inv.refresh_from_db()
                token = _invoice_link_signer.sign(f"{request.user.id}:{o.id}")
                row["invoice_url"] = request.build_absolute_uri(
                    f"/api/v1/user/orders/{o.id}/invoice/?token={token}"
                )
        except Exception:
            logger.exception("my_orders_paid_invoice_failed order_id=%s", o.pk)
        data.append(row)
    return envelope_response({"results": data})


@api_view(["GET"])
@permission_classes([AllowAny])
def order_invoice(request, pk: int):
    o = None
    inv = None

    if getattr(request, "user", None) and request.user.is_authenticated:
        o = Order.objects.filter(pk=pk, user=request.user).first()
        inv = GSTInvoice.objects.filter(order_id=o.pk).first() if o else None
    else:
        token = (request.query_params.get("token") or "").strip()
        if not token:
            return envelope_response(None, message="Unauthorized", success=False, status=401)
        try:
            payload = _invoice_link_signer.unsign(
                token, max_age=_INVOICE_LINK_MAX_AGE_SECONDS
            )
            user_id_s, order_id_s = payload.split(":", 1)
            if int(order_id_s) != int(pk):
                raise BadSignature("token does not match order")
            o = Order.objects.filter(pk=pk, user_id=int(user_id_s)).first()
            inv = GSTInvoice.objects.filter(order_id=o.pk).first() if o else None
        except (BadSignature, SignatureExpired, ValueError):
            return envelope_response(None, message="Unauthorized", success=False, status=401)

    if not o or not inv:
        return envelope_response(None, message="No invoice", success=False, status=404)
    try:
        ensure_gst_invoice_pdf(o)
        inv.refresh_from_db()
    except Exception:
        logger.exception("order_invoice_pdf_prepare_failed order_id=%s", o.pk)

    names = getattr(inv.pdf_file, "name", "") or ""
    if not names.strip():
        # Fall back to legacy URL if no generated/stored file exists.
        legacy = _invoice_pdf_url(request, inv)
        if legacy:
            return envelope_response(
                {"invoice_number": inv.invoice_number, "invoice_url": legacy}
            )
        return envelope_response(None, message="No invoice", success=False, status=404)

    try:
        fh = inv.pdf_file.open("rb")
    except Exception:
        logger.exception("order_invoice_pdf_open_failed order_id=%s", o.pk)
        return envelope_response(None, message="Invoice unavailable", success=False, status=500)

    filename = f"invoice-{inv.invoice_number or o.order_number or o.pk}.pdf"
    return FileResponse(fh, as_attachment=True, filename=filename, content_type="application/pdf")


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
