import json
import logging
import secrets
from collections import defaultdict

from django.conf import settings
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.http import FileResponse
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated

from apps.audit.services import write_audit
from apps.common.permissions import IsFinanceAdmin, IsSuperAdmin
from apps.common.responses import envelope_response
from apps.common.url_utils import public_absolute_uri
from apps.finance.services.aggregates import build_gst_report, build_revenue_rollup, orders_finance_page
from apps.finance.services.date_range import parse_finance_range
from apps.courses.models import EBook

from .models import GSTInvoice, Order, RefundRequest
from .services import (
    create_checkout_order,
    ensure_gst_invoice_pdf,
    finalize_order_as_paid,
    get_razorpay_key_id,
    latest_applicable_refund_status,
    normalize_billing_from_payload,
    refund_submission_blocked,
    remaining_refundable_for_order,
    remaining_refundable_for_order_line,
    resolve_gateway_payment_method,
    submit_member_refund_request,
    submit_member_refund_requests_for_lines,
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

    try:
        absolute = public_absolute_uri(request, rel_url)
    except Exception:
        logger.exception(
            "invoice_build_absolute_uri_failed invoice_id=%s rel=%s",
            inv.pk,
            rel_url,
        )
        return rel_url if rel_url else legacy
    return absolute or rel_url or legacy


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
    try:
        absolute = public_absolute_uri(request, rel_url)
    except Exception:
        logger.exception(
            "ebook_thumbnail_build_absolute_uri_failed ebook_id=%s rel=%s",
            ebook.pk,
            rel_url,
        )
        return rel_url or None
    return absolute or rel_url or None


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
                "key_id": get_razorpay_key_id(),
                "status": order.status,
            }
        )
    return envelope_response(
        {
            "order_id": order.id,
            "order_number": order.order_number,
            "amount_paise": rz["amount"],
            "razorpay_order_id": rz["id"],
            "key_id": get_razorpay_key_id(),
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
    order.refresh_from_db()
    paid_at = order.paid_at
    return envelope_response(
        {
            "status": "PAID",
            "order_number": order.order_number,
            "amount": str(order.amount_paid),
            "payment_verified_at": paid_at.isoformat() if paid_at else None,
            "payment_method": resolve_gateway_payment_method(order, payment_id),
        }
    )


@api_view(["POST"])
@permission_classes([AllowAny])
def webhook(request):
    body = request.body
    sig = request.headers.get("X-Razorpay-Signature", "")
    if not verify_webhook_signature(body, sig):
        return envelope_response(None, message="Bad signature", success=False, status=400)
    payload = json.loads(body.decode("utf-8"))
    return envelope_response({"received": True, "event": payload.get("event")})


def _open_refund_ebook_title(rr: RefundRequest) -> str | None:
    if rr.order_line_id and rr.order_line:
        return rr.order_line.ebook.title if rr.order_line.ebook_id else None
    if rr.order.ebook_id and rr.order.ebook:
        return rr.order.ebook.title
    return None


def _primary_open_refund_row(open_list: list[RefundRequest]) -> RefundRequest | None:
    for r in open_list:
        if r.order_line_id is None:
            return r
    return open_list[0] if open_list else None


def _item_refund_status(rlist: list[RefundRequest], *, item_line_id: int | None) -> str | None:
    return latest_applicable_refund_status(rlist, item_line_id=item_line_id)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_orders(request):
    from decimal import Decimal as D

    qs = list(
        Order.objects.filter(
            user=request.user,
            status__in=(Order.Status.PAID, Order.Status.REFUNDED),
        )
        .select_related("ebook")
        .prefetch_related("lines__ebook")
        .order_by("-id")[:50]
    )
    order_ids = [o.id for o in qs]
    open_refunds_list = list(
        RefundRequest.objects.filter(
            user=request.user,
            order_id__in=order_ids,
            status__in=(
                RefundRequest.Status.PENDING,
                RefundRequest.Status.PROCESSING,
            ),
        )
        .select_related("order_line__ebook", "order__ebook")
        .order_by("id")
    )
    opens_by_order: dict[int, list[RefundRequest]] = defaultdict(list)
    for r in open_refunds_list:
        opens_by_order[r.order_id].append(r)

    refunds_by_order: dict[int, list[RefundRequest]] = defaultdict(list)
    for rr in (
        RefundRequest.objects.filter(user=request.user, order_id__in=order_ids)
        .select_related("order_line__ebook", "order__ebook")
        .order_by("-id")
    ):
        refunds_by_order[rr.order_id].append(rr)

    approved_latest_any_by_order: dict[int, RefundRequest] = {}
    for ar in RefundRequest.objects.filter(
        user=request.user,
        order_id__in=order_ids,
        status=RefundRequest.Status.APPROVED,
    ).order_by("-id"):
        if ar.order_id not in approved_latest_any_by_order:
            approved_latest_any_by_order[ar.order_id] = ar

    now = timezone.now()
    data = []
    for o in qs:
        purchased_at = o.paid_at or o.created_at
        in_refund_window = not (o.refund_eligible_until and now > o.refund_eligible_until)
        opens = opens_by_order.get(o.id, [])
        primary_open = _primary_open_refund_row(opens)

        items = []
        lines_sorted = sorted(o.lines.all(), key=lambda ln: ln.pk)
        if lines_sorted:
            for line in lines_sorted:
                eb = line.ebook
                rem_ln = remaining_refundable_for_order_line(o, line.pk)
                line_refund_st = _item_refund_status(refunds_by_order[o.id], item_line_id=line.pk)
                can_line = bool(
                    o.status == Order.Status.PAID
                    and in_refund_window
                    and rem_ln > D("0")
                    and line_refund_st != RefundRequest.Status.REJECTED
                    and not refund_submission_blocked(o.pk, target_order_line_id=line.pk)
                )
                items.append(
                    {
                        "order_line_id": line.pk,
                        "ebook_id": line.ebook_id,
                        "slug": eb.slug if eb else None,
                        "title": eb.title if eb else None,
                        "thumbnail_url": _ebook_thumbnail_url(request, eb),
                        "unit_base_price": str(line.unit_base_price),
                        "refundable_amount": str(rem_ln),
                        "can_refund": can_line,
                        "refund_status": _item_refund_status(
                            refunds_by_order[o.id], item_line_id=line.pk
                        ),
                    }
                )
        elif o.ebook_id:
            rem_full = remaining_refundable_for_order(o) if o.status == Order.Status.PAID else D("0")
            legacy_refund_st = _item_refund_status(refunds_by_order[o.id], item_line_id=None)
            can_legacy = bool(
                o.status == Order.Status.PAID
                and in_refund_window
                and rem_full > D("0")
                and legacy_refund_st != RefundRequest.Status.REJECTED
                and not refund_submission_blocked(o.pk, target_order_line_id=None)
            )
            items.append(
                {
                    "order_line_id": None,
                    "ebook_id": o.ebook_id,
                    "slug": o.ebook.slug if o.ebook else None,
                    "title": o.ebook.title if o.ebook else None,
                    "thumbnail_url": _ebook_thumbnail_url(request, o.ebook),
                    "unit_base_price": str(o.base_price),
                    "refundable_amount": str(rem_full),
                    "can_refund": can_legacy,
                    "refund_status": _item_refund_status(
                        refunds_by_order[o.id], item_line_id=None
                    ),
                }
            )

        if items:
            # False only when every line is explicitly false; true/null/omitted on any line keeps the order true.
            can_any = not all(item.get("can_refund") is False for item in items)
        else:
            can_any = False
            if o.status == Order.Status.PAID and in_refund_window:
                if remaining_refundable_for_order(o) > D("0") and not refund_submission_blocked(
                    o.pk, target_order_line_id=None
                ):
                    can_any = True

        row = {
            "id": o.id,
            "order_number": o.order_number,
            "status": o.status,
            "purchased_at": purchased_at.isoformat() if purchased_at else None,
            "ebook_title": o.ebook.title if o.ebook else None,
            "thumbnail_url": _ebook_thumbnail_url(request, o.ebook),
            "amount_paid": str(o.amount_paid),
            "order_refundable_amount": str(remaining_refundable_for_order(o))
            if o.status == Order.Status.PAID
            else "0",
            "items": items,
            "invoice_url": None,
            "refund_request": None,
            "open_refund_requests": [],
            "can_refund": can_any,
        }
        for r in opens:
            row["open_refund_requests"].append(
                {
                    "reference": r.reference,
                    "status": r.status,
                    "order_line_id": r.order_line_id,
                    "ebook_title": _open_refund_ebook_title(r),
                }
            )
        if primary_open:
            row["refund_request"] = {
                "reference": primary_open.reference,
                "status": primary_open.status,
                "order_line_id": primary_open.order_line_id,
                "ebook_title": _open_refund_ebook_title(primary_open),
            }
        elif o.status == Order.Status.REFUNDED:
            ar = approved_latest_any_by_order.get(o.id)
            if ar:
                row["refund_request"] = {
                    "reference": ar.reference,
                    "status": ar.status,
                    "razorpay_refund_id": ar.razorpay_refund_id or None,
                    "order_line_id": ar.order_line_id,
                    "ebook_title": _open_refund_ebook_title(ar),
                }
        try:
            inv = GSTInvoice.objects.filter(order_id=o.id).first()
            if inv:
                ensure_gst_invoice_pdf(o)
                inv.refresh_from_db()
                token = _invoice_link_signer.sign(f"{request.user.id}:{o.id}")
                row["invoice_url"] = public_absolute_uri(
                    request, f"/api/v1/user/orders/{o.id}/invoice/?token={token}"
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
    """Submit a refund request for admin approval (does not refund immediately)."""
    note = (request.data.get("note") or request.data.get("member_note") or "").strip()

    if "order_line_ids" in request.data:
        raw_batch = request.data.get("order_line_ids")
        if raw_batch is None:
            return envelope_response(
                None,
                message="order_line_ids must be a non-empty list of integers",
                success=False,
                errors={"detail": "invalid_order_line_ids"},
                status=400,
            )
        if not isinstance(raw_batch, (list, tuple)):
            return envelope_response(
                None,
                message="order_line_ids must be a list",
                success=False,
                errors={"detail": "invalid_order_line_ids"},
                status=400,
            )
        if len(raw_batch) == 0:
            return envelope_response(
                None,
                message="order_line_ids must be a non-empty list",
                success=False,
                errors={"detail": "invalid_order_line_ids"},
                status=400,
            )
        try:
            rows = submit_member_refund_requests_for_lines(
                user=request.user,
                order_id=int(pk),
                order_line_ids=list(raw_batch),
                member_note=note,
            )
        except ValueError as e:
            msg = str(e)
            if msg == "Order not found":
                return envelope_response(None, message=msg, success=False, status=404)
            return envelope_response(None, message=msg, success=False, status=400)
        return envelope_response(
            {
                "count": len(rows),
                "results": [
                    {
                        "reference": rr.reference,
                        "status": rr.status,
                        "created_at": rr.created_at.isoformat(),
                        "amount": str(rr.amount),
                        "order_line_id": rr.order_line_id,
                    }
                    for rr in rows
                ],
            }
        )

    raw_line = request.data.get("order_line_id")
    order_line_id = None
    if raw_line not in (None, ""):
        try:
            order_line_id = int(raw_line)
        except (TypeError, ValueError):
            return envelope_response(
                None,
                message="Invalid order_line_id",
                success=False,
                errors={"detail": "invalid_order_line_id"},
                status=400,
            )
    try:
        rr = submit_member_refund_request(
            user=request.user,
            order_id=int(pk),
            member_note=note,
            order_line_id=order_line_id,
        )
    except ValueError as e:
        msg = str(e)
        if msg == "Order not found":
            return envelope_response(None, message=msg, success=False, status=404)
        return envelope_response(None, message=msg, success=False, status=400)
    return envelope_response(
        {
            "reference": rr.reference,
            "status": rr.status,
            "created_at": rr.created_at.isoformat(),
            "amount": str(rr.amount),
            "order_line_id": rr.order_line_id,
        }
    )


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
    """
    Paid orders in the finance date window (from/to or preset); pagination + search.
    """
    fr = parse_finance_range(request.query_params)
    q = (request.query_params.get("q") or "").strip()
    try:
        page = max(1, int(request.query_params.get("page", 1) or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(request.query_params.get("page_size", 20) or 20)
    except (TypeError, ValueError):
        page_size = 20
    page_size = max(1, min(page_size, 100))
    return envelope_response(
        orders_finance_page(
            d0=fr.date_from,
            d1=fr.date_to,
            q=q,
            page=page,
            page_size=page_size,
        )
    )


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def admin_revenue(request):
    fr = parse_finance_range(request.query_params)
    return envelope_response(build_revenue_rollup(fr))


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def admin_gst_report(request):
    fr = parse_finance_range(request.query_params)
    return envelope_response(build_gst_report(fr))
