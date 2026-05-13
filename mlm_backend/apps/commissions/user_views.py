import csv

from django.http import HttpResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request

from apps.admin_panel.utils import get_system_config
from apps.common.permissions import IsFinanceAdmin
from apps.common.responses import envelope_response
from apps.wallet.services.member_money import (
    build_commissions_summary,
    build_earnings_response,
    get_wallet_row,
)

from .admin_ledger_services import (
    AdminCommissionFilters,
    apply_admin_commission_filters,
    base_ledger_queryset,
    build_admin_commission_summary,
    commission_level_label,
    display_status_for_ledger,
    parse_admin_commission_filters,
    parse_pagination,
    serialize_admin_commission_detail,
    serialize_admin_commission_row,
)
from .commissions_report_pdf import build_commissions_report_pdf_bytes
from .held_release_service import release_held_commissions_for_user
from .services import build_user_milestones_dashboard


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def user_earnings_bundle(request: Request):
    """Consolidated earnings: overview and/or paginated ledger (see GET /api/v1/user/earnings/)."""
    include = request.query_params.get("include")
    period = request.query_params.get("period", "all") or "all"
    typ = request.query_params.get("type", "all") or "all"
    try:
        page = max(1, int(request.query_params.get("page", "1") or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(request.query_params.get("page_size", "20") or 20)
    except (TypeError, ValueError):
        page_size = 20
    data = build_earnings_response(
        request.user,
        include_raw=include,
        period=period,
        ledger_type=typ,
        page=page,
        page_size=page_size,
    )
    return envelope_response(data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def user_commissions(request):
    qs = (
        request.user.commissions_received.select_related("source_user", "order")
        .order_by("-id")[:100]
    )
    data = [
        {
            "id": x.id,
            "type": x.commission_type,
            "amount": str(x.amount),
            "net": str(x.net_amount),
            "status": x.status,
            "created_at": x.created_at.isoformat(),
        }
        for x in qs
    ]
    return envelope_response({"results": data})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def user_commissions_summary(request: Request):
    u = request.user
    cfg = get_system_config()
    wallet = get_wallet_row(u)
    return envelope_response(build_commissions_summary(u, cfg, wallet))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def user_milestones(request):
    return envelope_response(build_user_milestones_dashboard(request.user))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def user_tds(request):
    from apps.wallet.models import Wallet

    w, _ = Wallet.objects.get_or_create(user=request.user)
    return envelope_response(
        {
            "total_tds": str(w.total_tds_deducted),
            "fy_band_cash": str(w.band_cash_withdrawn_fy),
        }
    )


def _admin_commission_filter_base(request: Request) -> AdminCommissionFilters:
    return parse_admin_commission_filters(request.query_params)


def _export_row_cells(row) -> list[str]:
    return [
        str(row.id),
        row.created_at.isoformat(),
        row.order.order_number,
        row.order.razorpay_order_id or "",
        row.recipient.member_id,
        row.recipient.full_name,
        row.source_user.member_id,
        commission_level_label(row.commission_type) or "",
        row.commission_type,
        str(row.amount),
        str(row.tds_deducted),
        str(row.net_amount),
        row.status,
        display_status_for_ledger(row),
    ]


_EXPORT_HEADERS = [
    "id",
    "created_at",
    "order_number",
    "razorpay_order_id",
    "earner_member_id",
    "earner_name",
    "buyer_member_id",
    "level",
    "commission_type",
    "gross",
    "tds_deducted",
    "net_amount",
    "status",
    "status_display",
]


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def admin_commissions_summary(request: Request):
    flt = _admin_commission_filter_base(request)
    return envelope_response(build_admin_commission_summary(flt))


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def admin_commission_detail(request: Request, pk: int):
    row = base_ledger_queryset().filter(pk=pk).first()
    if not row:
        return envelope_response(None, message="Not found", success=False, status=404)
    return envelope_response(serialize_admin_commission_detail(row))


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def admin_commissions(request):
    flt = _admin_commission_filter_base(request)
    page, page_size = parse_pagination(request.query_params)
    qs = apply_admin_commission_filters(base_ledger_queryset(), flt).order_by("-id")
    total = qs.count()
    start = (page - 1) * page_size
    slice_qs = qs[start : start + page_size]
    data = {
        "count": total,
        "page": page,
        "page_size": page_size,
        "results": [serialize_admin_commission_row(x) for x in slice_qs],
    }
    return envelope_response(data)


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def admin_commissions_pending(request):
    """
    Legacy-friendly pending slice (PENDING + HELD), max 100 rows, same row shape as GET .../commissions/.
    Prefer GET /api/v1/admin/commissions/?status=pending for pagination.
    """
    base = parse_admin_commission_filters(request.query_params)
    flt = AdminCommissionFilters(
        q=base.q,
        status="pending",
        level=base.level,
        exclude_milestone=base.exclude_milestone,
    )
    qs = apply_admin_commission_filters(base_ledger_queryset(), flt).order_by("-id")[:100]
    return envelope_response(
        {
            "results": [serialize_admin_commission_row(x) for x in qs],
            "note": "Prefer GET /api/v1/admin/commissions/?status=pending for paginated results.",
        }
    )


@api_view(["POST"])
@permission_classes([IsFinanceAdmin])
def admin_force_credit(request):
    """
    Release HELD/PENDING (net=0) book commission rows for one recipient after admin review.
    Body: { "user_id": <int> } — required. Does not run automatically on KYC approval.
    """
    raw = request.data.get("user_id")
    try:
        uid = int(raw)
    except (TypeError, ValueError):
        return envelope_response(
            None,
            message="user_id is required and must be an integer.",
            success=False,
            errors={"detail": "invalid_user_id"},
            status=400,
        )
    if uid <= 0:
        return envelope_response(
            None,
            message="user_id is required and must be an integer.",
            success=False,
            errors={"detail": "invalid_user_id"},
            status=400,
        )

    out = release_held_commissions_for_user(user_id=uid, actor=request.user)
    if not out.get("ok"):
        return envelope_response(
            out,
            message="User not found.",
            success=False,
            status=404,
        )
    return envelope_response(out)


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def admin_tds_report(request):
    return envelope_response({"month": "2026-04", "tds": "0.00"})


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def admin_commissions_export(request):
    # Avoid query param name `format` — DRF uses it for content negotiation and can break routing.
    fmt = (request.query_params.get("export_format") or "csv").strip().lower()
    if fmt not in ("csv", "pdf"):
        return envelope_response(
            None,
            message="Invalid export_format. Use export_format=csv or export_format=pdf.",
            success=False,
            errors={"detail": "invalid_export_format"},
            status=400,
        )
    flt = _admin_commission_filter_base(request)
    qs = apply_admin_commission_filters(base_ledger_queryset(), flt).order_by("-id")

    if fmt == "csv":
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="commissions.csv"'
        w = csv.writer(resp)
        w.writerow(_EXPORT_HEADERS)
        for row in qs.iterator():
            w.writerow(_export_row_cells(row))
        return resp

    rows = (_export_row_cells(x) for x in qs.iterator())
    pdf_bytes = build_commissions_report_pdf_bytes(
        title="Commission ledger export",
        headers=_EXPORT_HEADERS,
        rows=rows,
    )
    resp = HttpResponse(pdf_bytes, content_type="application/pdf")
    resp["Content-Disposition"] = 'attachment; filename="commissions.pdf"'
    return resp
