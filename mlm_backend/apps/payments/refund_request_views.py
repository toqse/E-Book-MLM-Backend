import logging
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from apps.admin_panel.utils import get_system_config
from apps.audit.services import write_audit
from apps.commissions.models import CommissionLedger
from apps.common.permissions import IsFinanceAdmin
from apps.common.responses import envelope_response

from .models import Order, RefundRequest
from .services import (
    RazorpayRefundError,
    apply_approved_refund_fulfillment,
    refund_razorpay_payment_for_order,
)

logger = logging.getLogger(__name__)


def _serialize_refund_row(rr: RefundRequest) -> dict:
    inv = getattr(rr.order, "gst_invoice", None)
    inv_num = inv.invoice_number if inv else rr.order.gst_invoice_number
    purchased = rr.order.paid_at or rr.order.created_at
    line = getattr(rr, "order_line", None)
    ebook_title = None
    ebook_id = None
    if line and line.ebook_id:
        ebook_id = line.ebook_id
        ebook_title = line.ebook.title
    elif rr.order.ebook_id:
        ebook_id = rr.order.ebook_id
        ebook_title = rr.order.ebook.title if rr.order.ebook else None
    return {
        "id": rr.id,
        "reference": rr.reference,
        "member": {
            "full_name": rr.user.full_name,
            "member_id": rr.user.member_id,
            "phone": rr.user.phone,
        },
        "order_id": rr.order_id,
        "order_line_id": rr.order_line_id,
        "ebook_id": ebook_id,
        "ebook_title": ebook_title,
        "invoice_number": inv_num,
        "purchase_date": purchased.date().isoformat() if purchased else None,
        "amount": str(rr.amount),
        "payment_method": rr.payment_method,
        "status": rr.status,
        "member_note": rr.member_note or None,
        "reject_reason": rr.reject_reason or None,
        "created_at": rr.created_at.isoformat(),
        "processing_at": rr.processing_at.isoformat() if rr.processing_at else None,
        "approved_at": rr.approved_at.isoformat() if rr.approved_at else None,
        "rejected_at": rr.rejected_at.isoformat() if rr.rejected_at else None,
        "razorpay_refund_id": rr.razorpay_refund_id or None,
    }


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def admin_refunds_summary(request):
    cfg = get_system_config()
    sla_h = int(cfg.refund_request_sla_hours or 48)
    now = timezone.now()
    open_statuses = (RefundRequest.Status.PENDING, RefundRequest.Status.PROCESSING)
    open_qs = RefundRequest.objects.filter(status__in=open_statuses)

    pending_review = open_qs.count()
    sla_deadline = now - timedelta(hours=sla_h)
    open_list = list(open_qs.values_list("created_at", flat=True))
    within_sla = sum(1 for c in open_list if c >= sla_deadline)
    overdue_sla = pending_review - within_sla

    today = timezone.localdate()
    approved_today_qs = RefundRequest.objects.filter(
        status=RefundRequest.Status.APPROVED,
        approved_at__date=today,
    )
    approved_today_count = approved_today_qs.count()
    approved_today_sum = (
        approved_today_qs.aggregate(s=Sum("amount"))["s"] or Decimal("0")
    )

    month_start = timezone.localtime(now).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    if timezone.is_naive(month_start):
        month_start = timezone.make_aware(month_start, timezone.get_current_timezone())
    approved_month_order_ids = RefundRequest.objects.filter(
        status=RefundRequest.Status.APPROVED,
        approved_at__gte=month_start,
    ).values_list("order_id", flat=True)
    commission_reversed_month = CommissionLedger.objects.filter(
        order_id__in=approved_month_order_ids,
        status=CommissionLedger.Status.REVERSED,
    ).aggregate(s=Sum("net_amount"))["s"] or Decimal("0")

    cutoff_30d = now - timedelta(days=30)
    recent_done = RefundRequest.objects.filter(
        status=RefundRequest.Status.APPROVED,
        approved_at__gte=cutoff_30d,
        approved_at__isnull=False,
    ).values_list("created_at", "approved_at")
    hours_list = []
    for created_at, approved_at in recent_done:
        if created_at and approved_at:
            hours_list.append((approved_at - created_at).total_seconds() / 3600.0)
    avg_processing_hours = (
        round(sum(hours_list) / len(hours_list), 1) if hours_list else None
    )

    return envelope_response(
        {
            "pending_review": pending_review,
            "approved_today": {
                "count": approved_today_count,
                "amount_refunded": str(approved_today_sum),
            },
            "sla": {
                "hours": sla_h,
                "open_total": pending_review,
                "open_within_sla": within_sla,
                "open_overdue": overdue_sla,
            },
            "commission_reversed_this_month": str(commission_reversed_month),
            "avg_processing_hours_last_30d": avg_processing_hours,
        }
    )


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def admin_refunds_list(request):
    raw_status = (request.query_params.get("status") or "").strip().upper()
    q = (request.query_params.get("q") or "").strip()
    page = int(request.query_params.get("page", 1) or 1)
    page_size = int(request.query_params.get("page_size", 50) or 50)
    page = max(page, 1)
    page_size = min(max(page_size, 1), 200)

    qs = RefundRequest.objects.select_related(
        "order",
        "user",
        "order__gst_invoice",
        "order_line",
        "order_line__ebook",
        "order__ebook",
    ).order_by("-id")
    if raw_status:
        if raw_status not in set(RefundRequest.Status.values):
            return envelope_response(
                None,
                message="Invalid status filter.",
                success=False,
                errors={"detail": "invalid_status"},
                status=400,
            )
        qs = qs.filter(status=raw_status)
    if q:
        qs = qs.filter(
            Q(user__member_id__icontains=q)
            | Q(user__full_name__icontains=q)
            | Q(reference__icontains=q)
            | Q(order__gst_invoice__invoice_number__icontains=q)
        )

    total = qs.count()
    offset = (page - 1) * page_size
    rows = qs[offset : offset + page_size]
    results = [_serialize_refund_row(rr) for rr in rows]
    return envelope_response(
        {
            "count": total,
            "page": page,
            "page_size": page_size,
            "results": results,
        }
    )


@api_view(["POST"])
@permission_classes([IsFinanceAdmin])
def admin_refund_mark_processing(request, pk: int):
    rr = RefundRequest.objects.filter(pk=pk).first()
    if not rr:
        return envelope_response(None, message="Not found", success=False, status=404)
    if rr.status in (RefundRequest.Status.APPROVED, RefundRequest.Status.REJECTED):
        return envelope_response(
            None,
            message="Refund request is already closed.",
            success=False,
            errors={"detail": "invalid_state", "status": rr.status},
            status=400,
        )
    if rr.status == RefundRequest.Status.PROCESSING:
        return envelope_response(
            {
                "id": rr.id,
                "status": rr.status,
                "processing_at": rr.processing_at.isoformat() if rr.processing_at else None,
            }
        )
    now = timezone.now()
    rr.status = RefundRequest.Status.PROCESSING
    rr.processing_at = now
    rr.processing_by = request.user
    rr.save(
        update_fields=[
            "status",
            "processing_at",
            "processing_by",
            "updated_at",
        ]
    )
    return envelope_response(
        {
            "id": rr.id,
            "status": rr.status,
            "processing_at": rr.processing_at.isoformat() if rr.processing_at else None,
        }
    )


@api_view(["POST"])
@permission_classes([IsFinanceAdmin])
def admin_refund_approve(request, pk: int):
    rr = (
        RefundRequest.objects.select_related("order", "order_line", "order_line__ebook")
        .filter(pk=pk)
        .first()
    )
    if not rr:
        return envelope_response(None, message="Not found", success=False, status=404)
    if rr.status in (RefundRequest.Status.APPROVED, RefundRequest.Status.REJECTED):
        return envelope_response(
            None,
            message="Refund request is already closed.",
            success=False,
            errors={"detail": "invalid_state", "status": rr.status},
            status=400,
        )
    order = rr.order
    if order.status != Order.Status.PAID:
        return envelope_response(
            None,
            message="Order is not PAID; cannot approve refund.",
            success=False,
            errors={"detail": "order_not_paid", "order_status": order.status},
            status=400,
        )

    try:
        rz_refund_id = refund_razorpay_payment_for_order(
            order,
            refund_reference=rr.reference,
            amount_inr=rr.amount,
        )
    except RazorpayRefundError as exc:
        return envelope_response(
            None,
            message=exc.message,
            success=False,
            errors={"detail": "razorpay_refund_failed"},
            status=exc.status_code,
        )

    try:
        with transaction.atomic():
            rr_locked = RefundRequest.objects.select_for_update().filter(pk=pk).first()
            if not rr_locked:
                raise RuntimeError("Refund request vanished during approval")
            if rr_locked.status in (
                RefundRequest.Status.APPROVED,
                RefundRequest.Status.REJECTED,
            ):
                raise RuntimeError("Refund request was closed concurrently")
            order_locked = (
                Order.objects.select_for_update().filter(pk=rr_locked.order_id).first()
            )
            if not order_locked:
                raise RuntimeError("Order vanished during approval")
            if order_locked.status != Order.Status.PAID:
                raise RuntimeError(
                    f"Order no longer PAID (got {order_locked.status}); abort after Razorpay"
                )
            apply_approved_refund_fulfillment(
                order=order_locked,
                rr=rr_locked,
                actor=request.user,
                razorpay_refund_id=rz_refund_id,
            )
            now = timezone.now()
            rr_locked.status = RefundRequest.Status.APPROVED
            rr_locked.approved_at = now
            rr_locked.approved_by = request.user
            rr_locked.razorpay_refund_id = rz_refund_id
            rr_locked.save(
                update_fields=[
                    "status",
                    "approved_at",
                    "approved_by",
                    "razorpay_refund_id",
                    "updated_at",
                ]
            )
    except Exception:
        if rz_refund_id:
            write_audit(
                "refund.razorpay_ok_db_failed",
                actor=request.user,
                target_type="RefundRequest",
                target_id=str(pk),
                payload={
                    "order_id": rr.order_id,
                    "razorpay_refund_id": rz_refund_id,
                    "payment_id": (order.razorpay_payment_id or "")[:40],
                },
            )
        logger.exception("refund_approve_db_failed refund_request_id=%s", pk)
        return envelope_response(
            None,
            message="Refund was processed at Razorpay but could not finalize internally. Support has been notified.",
            success=False,
            errors={"detail": "refund_finalize_failed", "razorpay_refund_id": rz_refund_id},
            status=500,
        )

    rr = RefundRequest.objects.get(pk=pk)
    return envelope_response(
        {
            "id": rr.id,
            "status": rr.status,
            "approved_at": rr.approved_at.isoformat() if rr.approved_at else None,
            "razorpay_refund_id": rr.razorpay_refund_id,
        }
    )


@api_view(["POST"])
@permission_classes([IsFinanceAdmin])
def admin_refund_reject(request, pk: int):
    reason = (request.data.get("reason") or "").strip()
    with transaction.atomic():
        rr = RefundRequest.objects.select_for_update().filter(pk=pk).first()
        if not rr:
            return envelope_response(None, message="Not found", success=False, status=404)
        if rr.status in (RefundRequest.Status.APPROVED, RefundRequest.Status.REJECTED):
            return envelope_response(
                None,
                message="Refund request is already closed.",
                success=False,
                errors={"detail": "invalid_state", "status": rr.status},
                status=400,
            )
        now = timezone.now()
        rr.status = RefundRequest.Status.REJECTED
        rr.rejected_at = now
        rr.rejected_by = request.user
        rr.reject_reason = reason
        rr.save(
            update_fields=[
                "status",
                "rejected_at",
                "rejected_by",
                "reject_reason",
                "updated_at",
            ]
        )
    return envelope_response({"id": rr.id, "status": rr.status})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def user_refund_requests(request):
    qs = (
        RefundRequest.objects.filter(user=request.user)
        .select_related("order", "order__gst_invoice", "order_line", "order_line__ebook", "order__ebook")
        .order_by("-id")[:100]
    )
    out = []
    for rr in qs:
        inv = getattr(rr.order, "gst_invoice", None)
        inv_num = inv.invoice_number if inv else rr.order.gst_invoice_number
        purchased = rr.order.paid_at or rr.order.created_at
        line = rr.order_line
        ebook_title = None
        ebook_id = None
        if line and line.ebook_id:
            ebook_id = line.ebook_id
            ebook_title = line.ebook.title
        elif rr.order.ebook_id:
            ebook_id = rr.order.ebook_id
            ebook_title = rr.order.ebook.title if rr.order.ebook else None
        out.append(
            {
                "id": rr.id,
                "reference": rr.reference,
                "order_id": rr.order_id,
                "order_number": rr.order.order_number,
                "order_line_id": rr.order_line_id,
                "ebook_id": ebook_id,
                "ebook_title": ebook_title,
                "amount": str(rr.amount),
                "payment_method": rr.payment_method,
                "status": rr.status,
                "member_note": rr.member_note or None,
                "invoice_number": inv_num,
                "purchase_date": purchased.date().isoformat() if purchased else None,
                "reject_reason": rr.reject_reason if rr.status == RefundRequest.Status.REJECTED else None,
                "created_at": rr.created_at.isoformat(),
                "approved_at": rr.approved_at.isoformat() if rr.approved_at else None,
                "rejected_at": rr.rejected_at.isoformat() if rr.rejected_at else None,
                "razorpay_refund_id": rr.razorpay_refund_id or None,
            }
        )
    return envelope_response({"results": out})
