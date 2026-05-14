"""Aggregated metrics for Admin Finance (read-only querysets)."""

from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Count, Q, QuerySet, Sum
from django.utils import timezone

from apps.admin_panel.utils import get_system_config
from apps.audit.models import AuditLog
from apps.commissions.models import CommissionLedger, MilestoneRecord
from apps.courses.models import Enrollment
from apps.payments.models import GSTInvoice, Order, RefundRequest
from apps.sponsor_slots.models import SponsorSlotCode
from apps.wallet.models import WithdrawalRequest

from .date_range import FinanceDateRange, _indian_fy_bounds_for

Q2 = Decimal("0.01")
ZERO = Decimal("0")


def q2(x: Decimal | None) -> Decimal:
    return (x or ZERO).quantize(Q2)


def paid_orders_qs(d0: date, d1: date) -> QuerySet[Order]:
    return Order.objects.filter(
        status=Order.Status.PAID,
        paid_at__isnull=False,
        paid_at__date__gte=d0,
        paid_at__date__lte=d1,
    )


def _sum_amount_paid(qs: QuerySet[Order]) -> Decimal:
    return qs.aggregate(s=Sum("amount_paid"))["s"] or ZERO


def _sum_gateway(qs: QuerySet[Order]) -> Decimal:
    return qs.aggregate(s=Sum("gateway_charge"))["s"] or ZERO


def _sum_base(qs: QuerySet[Order]) -> Decimal:
    return qs.aggregate(s=Sum("base_price"))["s"] or ZERO


def _sum_gst_on_orders(qs: QuerySet[Order]) -> Decimal:
    return qs.aggregate(s=Sum("gst_amount"))["s"] or ZERO


def _commission_credited_net(d0: date, d1: date) -> Decimal:
    st = CommissionLedger.Status
    return (
        CommissionLedger.objects.filter(
            status=st.CREDITED,
            created_at__date__gte=d0,
            created_at__date__lte=d1,
        ).aggregate(s=Sum("net_amount"))["s"]
        or ZERO
    )


def _commission_gross_credited(d0: date, d1: date) -> Decimal:
    st = CommissionLedger.Status
    return (
        CommissionLedger.objects.filter(
            status=st.CREDITED,
            created_at__date__gte=d0,
            created_at__date__lte=d1,
        ).aggregate(s=Sum("amount"))["s"]
        or ZERO
    )


def _commission_tds(d0: date, d1: date) -> Decimal:
    st = CommissionLedger.Status
    return (
        CommissionLedger.objects.filter(
            status=st.CREDITED,
            created_at__date__gte=d0,
            created_at__date__lte=d1,
        ).aggregate(s=Sum("tds_deducted"))["s"]
        or ZERO
    )


def _milestone_net_credited(d0: date, d1: date) -> Decimal:
    return (
        MilestoneRecord.objects.filter(
            status="CREDITED",
            created_at__date__gte=d0,
            created_at__date__lte=d1,
        ).aggregate(s=Sum("net_bonus"))["s"]
        or ZERO
    )


def _milestone_tds(d0: date, d1: date) -> Decimal:
    return (
        MilestoneRecord.objects.filter(
            status="CREDITED",
            created_at__date__gte=d0,
            created_at__date__lte=d1,
        ).aggregate(s=Sum("tds_deducted"))["s"]
        or ZERO
    )


def _milestone_gross_credited(d0: date, d1: date) -> Decimal:
    return (
        MilestoneRecord.objects.filter(
            status="CREDITED",
            created_at__date__gte=d0,
            created_at__date__lte=d1,
        ).aggregate(s=Sum("bonus_amount"))["s"]
        or ZERO
    )


def _withdrawals_net_paid(d0: date, d1: date) -> Decimal:
    st = WithdrawalRequest.Status.PAID
    return (
        WithdrawalRequest.objects.filter(
            status=st,
            paid_at__isnull=False,
            paid_at__date__gte=d0,
            paid_at__date__lte=d1,
        ).aggregate(s=Sum("net_payable"))["s"]
        or ZERO
    )


def _withdrawals_tds(d0: date, d1: date) -> Decimal:
    st = WithdrawalRequest.Status.PAID
    return (
        WithdrawalRequest.objects.filter(
            status=st,
            paid_at__isnull=False,
            paid_at__date__gte=d0,
            paid_at__date__lte=d1,
        ).aggregate(s=Sum("tds_amount"))["s"]
        or ZERO
    )


def _refunds_approved_amount(d0: date, d1: date) -> Decimal:
    return (
        RefundRequest.objects.filter(
            status=RefundRequest.Status.APPROVED,
            approved_at__isnull=False,
            approved_at__date__gte=d0,
            approved_at__date__lte=d1,
        ).aggregate(s=Sum("amount"))["s"]
        or ZERO
    )


def _enrollment_count_for_orders(d0: date, d1: date) -> int:
    return Enrollment.objects.filter(
        order__status=Order.Status.PAID,
        order__paid_at__date__gte=d0,
        order__paid_at__date__lte=d1,
    ).count()


def _classify_stream(o: Order) -> str:
    lc = int(getattr(o, "line_count", 0) or 0)
    if o.is_retail_purchase:
        return "retail"
    if lc > 1:
        return "multi_cart"
    if o.is_sponsor_slot_redemption:
        return "sponsor_slot"
    return "mlm_standard"


def _income_bucket_totals(d0: date, d1: date) -> dict[str, Decimal]:
    qs = paid_orders_qs(d0, d1).annotate(line_count=Count("lines"))
    buckets: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for o in qs.iterator(chunk_size=500):
        key = _classify_stream(o)
        buckets[key] += o.amount_paid or ZERO
    return dict(buckets)


STREAM_META: list[tuple[str, str]] = [
    ("retail", "Retail purchases"),
    ("multi_cart", "Multi-title cart"),
    ("sponsor_slot", "Sponsor slot redemption"),
    ("mlm_standard", "MLM standard book sales"),
]


def _trend_pct(cur: Decimal, prev: Decimal) -> str | None:
    if prev > ZERO:
        return str(((cur - prev) / prev * Decimal("100")).quantize(Q2))
    if cur > ZERO:
        return None
    return None


def build_income_streams(fr: FinanceDateRange) -> dict[str, Any]:
    cur = _income_bucket_totals(fr.date_from, fr.date_to)
    prev = _income_bucket_totals(fr.previous_date_from, fr.previous_date_to)
    total_cur = sum(cur.values(), ZERO)
    rows = []
    for key, label in STREAM_META:
        amt = cur.get(key, ZERO)
        share = str(((amt / total_cur) * Decimal("100")).quantize(Q2)) if total_cur > ZERO else "0.00"
        rows.append(
            {
                "category_key": key,
                "label": label,
                "amount": str(q2(amt)),
                "share_percent": share,
                "trend_percent": _trend_pct(amt, prev.get(key, ZERO)),
            }
        )
    other_cur = total_cur - sum((cur.get(k, ZERO) for k, _ in STREAM_META), ZERO)
    other_prev = sum(prev.values(), ZERO) - sum((prev.get(k, ZERO) for k, _ in STREAM_META), ZERO)
    if other_cur > ZERO or other_prev > ZERO:
        share_o = (
            str(((other_cur / total_cur) * Decimal("100")).quantize(Q2)) if total_cur > ZERO else "0.00"
        )
        rows.append(
            {
                "category_key": "other",
                "label": "Other",
                "amount": str(q2(other_cur)),
                "share_percent": share_o,
                "trend_percent": _trend_pct(other_cur, other_prev),
            }
        )
    return {
        "range": {
            "from": fr.date_from.isoformat(),
            "to": fr.date_to.isoformat(),
            "previous_from": fr.previous_date_from.isoformat(),
            "previous_to": fr.previous_date_to.isoformat(),
        },
        "total_income": str(q2(total_cur)),
        "rows": rows,
    }


def _gst_invoices_in_range(d0: date, d1: date) -> Decimal:
    return (
        GSTInvoice.objects.filter(
            created_at__date__gte=d0,
            created_at__date__lte=d1,
        ).aggregate(s=Sum("total_gst"))["s"]
        or ZERO
    )


def _fy_monthly_gross_series(fy_start: date, fy_end: date) -> list[dict[str, Any]]:
    """12 months from fy_start (1 Apr) through fy_end (31 Mar)."""
    out: list[dict[str, Any]] = []
    y, m = fy_start.year, fy_start.month
    for _ in range(12):
        d0 = date(y, m, 1)
        last = monthrange(y, m)[1]
        d1 = date(y, m, last)
        g = _sum_amount_paid(paid_orders_qs(d0, d1))
        out.append({"month": f"{y:04d}-{m:02d}", "gross": str(q2(g))})
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def _payouts_by_method(d0: date, d1: date) -> dict[str, Decimal]:
    st = WithdrawalRequest.Status.PAID
    qs = WithdrawalRequest.objects.filter(
        status=st,
        paid_at__isnull=False,
        paid_at__date__gte=d0,
        paid_at__date__lte=d1,
    )
    agg = qs.values("payout_method").annotate(s=Sum("net_payable"))
    out: dict[str, Decimal] = {}
    for row in agg:
        out[row["payout_method"] or "UNKNOWN"] = row["s"] or ZERO
    return out


def _pending_withdrawals_net(d0: date, d1: date) -> Decimal:
    """Pending/processing withdrawals created in window (for 'pending' subtext)."""
    pend = (WithdrawalRequest.Status.PENDING, WithdrawalRequest.Status.PROCESSING, WithdrawalRequest.Status.APPROVED)
    return (
        WithdrawalRequest.objects.filter(
            status__in=pend,
            created_at__date__gte=d0,
            created_at__date__lte=d1,
        ).aggregate(s=Sum("net_payable"))["s"]
        or ZERO
    )


def _milestone_unlocked_count(d0: date, d1: date) -> int:
    return MilestoneRecord.objects.filter(
        status="CREDITED",
        created_at__date__gte=d0,
        created_at__date__lte=d1,
    ).count()


def _net_platform_in_window(d0: date, d1: date) -> Decimal:
    orders_cur = paid_orders_qs(d0, d1)
    gross = _sum_amount_paid(orders_cur)
    comm = _commission_credited_net(d0, d1)
    ms = _milestone_net_credited(d0, d1)
    wd = _withdrawals_net_paid(d0, d1)
    ref = _refunds_approved_amount(d0, d1)
    gw = _sum_gateway(orders_cur)
    return q2(gross - comm - ms - wd - ref - gw)


def _sponsor_active_stats() -> tuple[int, str]:
    cfg = get_system_config()
    unit = Decimal(str(cfg.product_base_price or 200))
    n = SponsorSlotCode.objects.filter(status=SponsorSlotCode.Status.ACTIVE).count()
    slot_value = q2(unit * Decimal(n))
    return n, str(slot_value)


def build_overview(fr: FinanceDateRange) -> dict[str, Any]:
    d0, d1 = fr.date_from, fr.date_to
    p0, p1 = fr.previous_date_from, fr.previous_date_to

    orders_cur = paid_orders_qs(d0, d1)
    orders_prev = paid_orders_qs(p0, p1)
    gross_cur = _sum_amount_paid(orders_cur)
    gross_prev = _sum_amount_paid(orders_prev)
    enroll = _enrollment_count_for_orders(d0, d1)
    avg = (gross_cur / Decimal(enroll)).quantize(Q2) if enroll else ZERO

    comm_net_cur = _commission_credited_net(d0, d1)
    comm_net_prev = _commission_credited_net(p0, p1)
    comm_gross_cur = _commission_gross_credited(d0, d1)

    ms_net_cur = _milestone_net_credited(d0, d1)
    ms_net_prev = _milestone_net_credited(p0, p1)
    ms_gross_cur = _milestone_gross_credited(d0, d1)

    wd_net_cur = _withdrawals_net_paid(d0, d1)
    wd_net_prev = _withdrawals_net_paid(p0, p1)
    refunds_cur = _refunds_approved_amount(d0, d1)
    gateway_cur = _sum_gateway(orders_cur)

    tds_comm = _commission_tds(d0, d1)
    tds_wd = _withdrawals_tds(d0, d1)
    tds_ms = _milestone_tds(d0, d1)
    tds_total = q2(tds_comm + tds_wd + tds_ms)

    # Documented net platform approximation (single source of truth for UI + exports).
    net_platform = _net_platform_in_window(d0, d1)
    net_prev = _net_platform_in_window(p0, p1)
    margin_pct = str(((net_platform / gross_cur) * Decimal("100")).quantize(Q2)) if gross_cur > ZERO else "0.00"

    gst_collected = _gst_invoices_in_range(d0, d1)
    if gst_collected <= ZERO:
        gst_collected = _sum_gst_on_orders(orders_cur)

    fy_start, fy_end = _indian_fy_bounds_for(d1)
    fy_label = f"{fy_start.year % 100:02d}-{(fy_end.year % 100):02d}"
    monthly = _fy_monthly_gross_series(fy_start, fy_end)

    payout_split = _payouts_by_method(d0, d1)
    pending_wd = _pending_withdrawals_net(d0, d1)

    active_slots, slot_value_str = _sponsor_active_stats()

    dist: list[dict[str, Any]] = []
    if gross_cur > ZERO:
        parts = [
            ("net_platform_income", net_platform, "Net platform income"),
            ("commission_distributed", comm_net_cur, "Commission distributed (net)"),
            ("milestone_bonuses", ms_net_cur, "Milestone bonuses (net)"),
            ("payouts", wd_net_cur, "Payouts processed (net)"),
            ("refunds", refunds_cur, "Refunds approved"),
            ("gateway", gateway_cur, "Gateway charges"),
        ]
        for key, amt, label in parts:
            pct = ((amt / gross_cur) * Decimal("100")).quantize(Q2) if gross_cur > ZERO else ZERO
            dist.append(
                {
                    "key": key,
                    "label": label,
                    "amount": str(q2(amt)),
                    "percent": str(pct),
                }
            )

    return {
        "range": {
            "from": d0.isoformat(),
            "to": d1.isoformat(),
            "preset": fr.preset,
            "previous_from": p0.isoformat(),
            "previous_to": p1.isoformat(),
        },
        "kpis": {
            "gross_revenue": {
                "amount_paid": str(q2(gross_cur)),
                "enrollments": enroll,
                "avg_per_enrollment": str(avg),
                "trend_percent_vs_previous": _trend_pct(gross_cur, gross_prev),
            },
            "commission_paid_net": {
                "amount": str(q2(comm_net_cur)),
                "gross_credited": str(q2(comm_gross_cur)),
                "trend_percent_vs_previous": _trend_pct(comm_net_cur, comm_net_prev),
            },
            "payouts_processed_net": {
                "amount": str(q2(wd_net_cur)),
                "by_method": {k: str(q2(v)) for k, v in payout_split.items()},
                "trend_percent_vs_previous": _trend_pct(wd_net_cur, wd_net_prev),
                "pending_net_created_in_range": str(q2(pending_wd)),
            },
            "net_platform_income": {
                "amount": str(net_platform),
                "margin_percent_of_gross": margin_pct,
                "trend_percent_vs_previous": _trend_pct(net_platform, net_prev),
            },
            "tds_deducted": {
                "total": str(tds_total),
                "from_commissions": str(q2(tds_comm)),
                "from_withdrawals": str(q2(tds_wd)),
                "from_milestones": str(q2(tds_ms)),
            },
            "milestone_bonuses": {
                "gross_credited": str(q2(ms_gross_cur)),
                "net_credited": str(q2(ms_net_cur)),
                "credited_rows": _milestone_unlocked_count(d0, d1),
                "trend_percent_vs_previous": _trend_pct(ms_net_cur, ms_net_prev),
            },
            "sponsor_slots": {
                "active_count": active_slots,
                "active_slot_value_proxy": slot_value_str,
                "note": "slot_value_proxy = active_count * system_config.product_base_price",
            },
            "gst_collected": {
                "amount": str(q2(gst_collected)),
                "source": "gst_invoice_sum_fallback_order_gst",
            },
        },
        "charts": {
            "monthly_revenue_trend": {
                "fy_label": fy_label,
                "fy_from": fy_start.isoformat(),
                "fy_to": fy_end.isoformat(),
                "months": monthly,
            },
            "revenue_distribution_percent_of_gross": dist,
        },
        "computed_at": timezone.now().isoformat(),
    }


def build_tds_detail(fr: FinanceDateRange) -> dict[str, Any]:
    d0, d1 = fr.date_from, fr.date_to
    wd_qs = WithdrawalRequest.objects.filter(
        status=WithdrawalRequest.Status.PAID,
        paid_at__isnull=False,
        paid_at__date__gte=d0,
        paid_at__date__lte=d1,
    )
    by_section: list[dict[str, Any]] = []
    for row in (
        wd_qs.exclude(tds_section__isnull=True)
        .exclude(tds_section="")
        .values("tds_section")
        .annotate(s=Sum("tds_amount"))
        .order_by("tds_section")
    ):
        by_section.append(
            {
                "section": row["tds_section"],
                "amount": str(q2(row["s"] or ZERO)),
            }
        )
    blank_sum = (
        wd_qs.filter(Q(tds_section__isnull=True) | Q(tds_section="")).aggregate(s=Sum("tds_amount"))["s"] or ZERO
    )
    if blank_sum > ZERO:
        by_section.append({"section": "(unspecified)", "amount": str(q2(blank_sum))})

    tds_comm = _commission_tds(d0, d1)
    tds_wd = _withdrawals_tds(d0, d1)
    tds_ms = _milestone_tds(d0, d1)
    total = q2(tds_comm + tds_wd + tds_ms)
    return {
        "range": {
            "from": d0.isoformat(),
            "to": d1.isoformat(),
        },
        "withdrawals_by_section": by_section,
        "commissions_tds_on_credited_rows": str(q2(tds_comm)),
        "milestones_tds_on_credited_rows": str(q2(tds_ms)),
        "withdrawals_tds_on_paid_payouts": str(q2(tds_wd)),
        "total_tds": str(total),
    }


def build_expenditure(fr: FinanceDateRange) -> dict[str, Any]:
    d0, d1 = fr.date_from, fr.date_to
    orders_cur = paid_orders_qs(d0, d1)
    gateway = _sum_gateway(orders_cur)
    refunds = _refunds_approved_amount(d0, d1)
    return {
        "range": {"from": d0.isoformat(), "to": d1.isoformat()},
        "rows": [
            {
                "category_key": "gateway",
                "label": "Payment gateway charges (on paid orders)",
                "amount": str(q2(gateway)),
            },
            {
                "category_key": "refunds_approved",
                "label": "Refunds approved (cash-out)",
                "amount": str(q2(refunds)),
            },
        ],
        "total": str(q2(gateway + refunds)),
    }


def build_gst_report(fr: FinanceDateRange) -> dict[str, Any]:
    d0, d1 = fr.date_from, fr.date_to
    rows = []
    for inv in (
        GSTInvoice.objects.filter(created_at__date__gte=d0, created_at__date__lte=d1)
        .select_related("order")
        .order_by("-created_at")[:500]
    ):
        rows.append(
            {
                "invoice_number": inv.invoice_number,
                "order_number": inv.order.order_number,
                "base_amount": str(q2(inv.base_amount)),
                "total_gst": str(q2(inv.total_gst)),
                "grand_total": str(q2(inv.grand_total)),
                "created_at": inv.created_at.isoformat(),
            }
        )
    collected = _gst_invoices_in_range(d0, d1)
    if collected <= ZERO:
        collected = _sum_gst_on_orders(paid_orders_qs(d0, d1))
    return {"collected": str(q2(collected)), "gstr1": rows}


def build_revenue_rollup(fr: FinanceDateRange | None = None) -> dict[str, Any]:
    """Backward-compatible keys for GET /api/v1/admin/revenue/ plus explicit range totals."""
    today = timezone.localdate()
    week_start = today - timedelta(days=6)
    month_start = today.replace(day=1)

    def window_total(d0: date, d1: date) -> Decimal:
        return _sum_amount_paid(paid_orders_qs(d0, d1))

    out: dict[str, Any] = {
        "today": str(q2(window_total(today, today))),
        "week": str(q2(window_total(week_start, today))),
        "month": str(q2(window_total(month_start, today))),
    }
    if fr is not None:
        out["range"] = {
            "from": fr.date_from.isoformat(),
            "to": fr.date_to.isoformat(),
            "gross_amount_paid": str(q2(window_total(fr.date_from, fr.date_to))),
            "paid_orders": paid_orders_qs(fr.date_from, fr.date_to).count(),
        }
    return out


def build_tds_report_rollup(fr: FinanceDateRange) -> dict[str, Any]:
    d0, d1 = fr.date_from, fr.date_to
    fy_start, fy_end = _indian_fy_bounds_for(d1)
    detail = build_tds_detail(fr)
    return {
        "fy": f"{fy_start.year % 100:02d}-{(fy_end.year % 100):02d}",
        "total": detail["total_tds"],
        "window": {"from": d0.isoformat(), "to": d1.isoformat()},
    }


def commission_ledger_count(fr: FinanceDateRange) -> int:
    return CommissionLedger.objects.filter(
        created_at__date__gte=fr.date_from,
        created_at__date__lte=fr.date_to,
    ).count()


def audit_trail_count(fr: FinanceDateRange) -> int:
    return AuditLog.objects.filter(
        created_at__date__gte=fr.date_from,
        created_at__date__lte=fr.date_to,
    ).count()


def build_tab_counts(fr: FinanceDateRange) -> dict[str, Any]:
    return {
        "commission_ledger": commission_ledger_count(fr),
        "audit_trail": audit_trail_count(fr),
    }


def _actor_label(log: AuditLog) -> str:
    if log.actor_id and log.actor:
        return log.actor.full_name or log.actor.member_id or f"user:{log.actor_id}"
    return "System"


def _audit_ref(log: AuditLog) -> str:
    pl = log.payload or {}
    for key in ("order_number", "refund_reference", "withdrawal_id", "reference"):
        v = pl.get(key)
        if v:
            return str(v)
    if log.target_id:
        return str(log.target_id)
    return ""


def build_audit_trail(
    fr: FinanceDateRange,
    *,
    page: int,
    page_size: int,
    q: str,
    finance_actions_only: bool,
) -> dict[str, Any]:
    d0, d1 = fr.date_from, fr.date_to
    qs = AuditLog.objects.select_related("actor").filter(
        created_at__date__gte=d0,
        created_at__date__lte=d1,
    )
    if finance_actions_only:
        qs = qs.filter(
            Q(action__startswith="payment.")
            | Q(action__startswith="refund.")
            | Q(action__startswith="commission.")
            | Q(action__startswith="withdrawal")
            | Q(action__startswith="payout")
            | Q(action__startswith="finance.")
        )
    if q:
        qs = qs.filter(
            Q(action__icontains=q)
            | Q(target_id__icontains=q)
            | Q(target_type__icontains=q)
        )
    total = qs.count()
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    start = (page - 1) * page_size
    slice_qs = qs.order_by("-created_at", "-id")[start : start + page_size]
    results = []
    for log in slice_qs:
        results.append(
            {
                "id": log.id,
                "actor": _actor_label(log),
                "action": log.action,
                "ref": _audit_ref(log),
                "timestamp": timezone.localtime(log.created_at).isoformat(),
                "target_type": log.target_type or None,
                "target_id": log.target_id or None,
            }
        )
    return {
        "count": total,
        "page": page,
        "page_size": page_size,
        "results": results,
    }


def finance_search(
    fr: FinanceDateRange,
    q: str,
    *,
    limit: int = 8,
) -> dict[str, Any]:
    q = (q or "").strip()
    if not q:
        return {"q": "", "commissions": [], "withdrawals": [], "orders": []}
    lim = max(1, min(limit, 25))
    from apps.commissions.admin_ledger_services import (
        apply_admin_commission_filters,
        base_ledger_queryset,
        parse_admin_commission_filters,
        serialize_admin_commission_row,
    )

    fdict = {
        "q": q,
        "from": fr.date_from.isoformat(),
        "to": fr.date_to.isoformat(),
    }
    cflt = parse_admin_commission_filters(fdict)
    cqs = apply_admin_commission_filters(base_ledger_queryset(), cflt).order_by("-id")[:lim]
    comm = [serialize_admin_commission_row(x) for x in cqs]

    from django.db.models import Q as QQ

    wqs = (
        WithdrawalRequest.objects.select_related("user")
        .filter(
            created_at__date__gte=fr.date_from,
            created_at__date__lte=fr.date_to,
        )
        .filter(Q(user__member_id__icontains=q) | Q(user__phone__icontains=q))
        .order_by("-id")[:lim]
    )
    wrows = [
        {
            "id": w.id,
            "member_id": w.user.member_id,
            "status": w.status,
            "net_payable": str(w.net_payable),
            "created_at": w.created_at.isoformat(),
        }
        for w in wqs
    ]

    oqs = (
        paid_orders_qs(fr.date_from, fr.date_to)
        .filter(Q(order_number__icontains=q) | Q(razorpay_order_id__icontains=q))
        .order_by("-id")[:lim]
    )
    orows = [
        {
            "id": o.id,
            "order_number": o.order_number,
            "status": o.status,
            "amount_paid": str(o.amount_paid),
            "paid_at": o.paid_at.isoformat() if o.paid_at else None,
        }
        for o in oqs
    ]
    return {"q": q, "commissions": comm, "withdrawals": wrows, "orders": orows}


def orders_finance_page(
    *,
    d0: date,
    d1: date,
    q: str,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    qs = paid_orders_qs(d0, d1).select_related("user", "ebook").annotate(line_count=Count("lines"))
    if q:
        qs = qs.filter(
            Q(order_number__icontains=q)
            | Q(razorpay_order_id__icontains=q)
            | Q(user__member_id__icontains=q)
            | Q(user__full_name__icontains=q)
        )
    total = qs.count()
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    start = (page - 1) * page_size
    rows = qs.order_by("-paid_at", "-id")[start : start + page_size]
    results = []
    for o in rows:
        results.append(
            {
                "id": o.id,
                "order_number": o.order_number,
                "status": o.status,
                "amount_paid": str(q2(o.amount_paid)),
                "base_price": str(q2(o.base_price)),
                "gst_amount": str(q2(o.gst_amount)),
                "gateway_charge": str(q2(o.gateway_charge)),
                "paid_at": o.paid_at.isoformat() if o.paid_at else None,
                "is_retail_purchase": o.is_retail_purchase,
                "is_sponsor_slot_redemption": o.is_sponsor_slot_redemption,
                "line_count": int(getattr(o, "line_count", 0) or 0),
                "user": {
                    "id": o.user_id,
                    "member_id": o.user.member_id,
                    "full_name": o.user.full_name,
                },
                "ebook_id": o.ebook_id,
                "ebook_title": o.ebook.title if o.ebook_id else None,
            }
        )
    return {"count": total, "page": page, "page_size": page_size, "results": results}
