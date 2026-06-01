"""Admin Commission Engine: shared queryset filters, serialization, and aggregates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from django.db.models import Q, QuerySet, Sum
from django.utils import timezone as djtz
from django.utils.dateparse import parse_date as django_parse_date

from apps.admin_panel.utils import get_system_config
from apps.payments.models import Order
from apps.users.models import User
from apps.wallet.models import Wallet

from .models import CommissionLedger

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


@dataclass(frozen=True)
class AdminCommissionFilters:
    q: str
    status: str  # all | processed | pending | reversed | held
    level: str  # all | L1 | L2 | L3 | L4 | passive
    exclude_milestone: bool
    date_from: date | None
    date_to: date | None


def _norm(s: str | None) -> str:
    return (s or "").strip()


def parse_admin_commission_filters(query_params: dict[str, Any]) -> AdminCommissionFilters:
    raw_status = _norm(query_params.get("status")).lower() or "all"
    if raw_status not in ("all", "processed", "pending", "reversed", "held"):
        raw_status = "all"
    raw_level = _norm(query_params.get("level")).upper()
    if raw_level == "ALL" or not raw_level:
        raw_level = "all"
    else:
        raw_level = raw_level.lower()
        if raw_level not in ("l1", "l2", "l3", "l4", "passive"):
            raw_level = "all"
    em = query_params.get("exclude_milestone")
    if em is None:
        exclude_milestone = True
    else:
        s = str(em).strip().lower()
        exclude_milestone = s not in ("0", "false", "no", "off", "")
    raw_df = _norm(query_params.get("from"))
    raw_dt = _norm(query_params.get("to"))
    d_from = django_parse_date(raw_df) if raw_df else None
    d_to = django_parse_date(raw_dt) if raw_dt else None
    if d_from and d_to and d_from > d_to:
        d_from, d_to = d_to, d_from
    return AdminCommissionFilters(
        q=_norm(query_params.get("q")),
        status=raw_status,
        level=raw_level,
        exclude_milestone=exclude_milestone,
        date_from=d_from,
        date_to=d_to,
    )


def base_ledger_queryset() -> QuerySet[CommissionLedger]:
    return CommissionLedger.objects.select_related("recipient", "source_user", "order")


def apply_admin_commission_filters(
    qs: QuerySet[CommissionLedger], flt: AdminCommissionFilters
) -> QuerySet[CommissionLedger]:
    if flt.exclude_milestone:
        qs = qs.exclude(commission_type=CommissionLedger.CommissionType.MILESTONE)
    if flt.q:
        term = flt.q
        qs = qs.filter(
            Q(recipient__full_name__icontains=term)
            | Q(recipient__member_id__icontains=term)
            | Q(order__order_number__icontains=term)
            | Q(order__razorpay_order_id__icontains=term)
        )
    ct = CommissionLedger.CommissionType
    if flt.level == "l1":
        qs = qs.filter(commission_type=ct.DIRECT)
    elif flt.level == "l2":
        qs = qs.filter(commission_type=ct.UPLINE_L2)
    elif flt.level == "l3":
        qs = qs.filter(commission_type=ct.UPLINE_L3)
    elif flt.level == "l4":
        # Backward compatible: l4 maps to passive UPLINE_L3 after rename.
        qs = qs.filter(commission_type=ct.UPLINE_L3)
    elif flt.level == "passive":
        qs = qs.filter(
            commission_type__in=(ct.UPLINE_L1, ct.UPLINE_L2, ct.UPLINE_L3),
        )
    st = CommissionLedger.Status
    if flt.status == "processed":
        qs = qs.filter(status=st.CREDITED)
    elif flt.status == "pending":
        qs = qs.filter(status__in=(st.PENDING, st.HELD))
    elif flt.status == "reversed":
        qs = qs.filter(status=st.REVERSED)
    elif flt.status == "held":
        qs = qs.filter(status=st.HELD)
    if flt.date_from is not None:
        qs = qs.filter(created_at__date__gte=flt.date_from)
    if flt.date_to is not None:
        qs = qs.filter(created_at__date__lte=flt.date_to)
    return qs


def commission_level_label(commission_type: str) -> str | None:
    if commission_type == CommissionLedger.CommissionType.DIRECT:
        return "L1"
    mapping = {
        CommissionLedger.CommissionType.UPLINE_L1: "L1",
        CommissionLedger.CommissionType.UPLINE_L2: "L2",
        CommissionLedger.CommissionType.UPLINE_L3: "L3",
        CommissionLedger.CommissionType.MILESTONE: "MS",
    }
    return mapping.get(commission_type)


def display_status_for_ledger(row: CommissionLedger) -> str:
    st = CommissionLedger.Status
    if row.status == st.REVERSED:
        return "Reversed"
    if row.status == st.CREDITED:
        return "Processed"
    if row.status in (st.PENDING, st.HELD):
        return "Pending"
    return row.status


def _ordinal_suffix(day: int) -> str:
    if 11 <= (day % 100) <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


def _format_human_dt(dt) -> str:
    """Format a datetime as e.g. '10.40 AM, 23rd May 2026' in the project TZ."""
    if dt is None:
        return ""
    local = djtz.localtime(dt) if djtz.is_aware(dt) else dt
    hour12 = local.strftime("%I").lstrip("0") or "12"
    minute = local.strftime("%M")
    meridiem = local.strftime("%p")
    day = local.day
    month_year = local.strftime("%B %Y")
    return f"{hour12}.{minute} {meridiem}, {day}{_ordinal_suffix(day)} {month_year}"


def _build_admin_commission_note(row: CommissionLedger) -> str:
    """Human-readable note for admin commission detail (derived at read time)."""
    st = CommissionLedger.Status
    ct = CommissionLedger.CommissionType
    recipient = row.recipient
    order = row.order
    created_at = _format_human_dt(row.created_at)

    if row.status == st.REVERSED:
        return (
            "Reversed: commission rolled back "
            "(typically due to order refund or admin reversal)."
        )

    if row.status in (st.HELD, st.PENDING):
        if order.status != Order.Status.PAID:
            return (
                f"Held: order {order.order_number} is currently {order.status}; "
                "commission cannot be released."
            )
        first_approved_at = recipient.kyc_first_approved_at
        if first_approved_at is None:
            return (
                f"{recipient.full_name}'s KYC was not approved when this commission "
                f"was generated at {created_at}. Will release after first KYC approval."
            )
        if row.created_at < first_approved_at:
            return (
                f"Forfeited: this commission was generated at {created_at}, before "
                f"{recipient.full_name}'s first KYC approval at "
                f"{_format_human_dt(first_approved_at)}. Per policy, "
                "pre-first-approval commissions are not credited."
            )
        if recipient.kyc_status != User.KYCStatus.VERIFIED:
            return (
                f"Held: {recipient.full_name}'s KYC is currently "
                f"{recipient.kyc_status}. Credit will release once KYC is re-verified."
            )
        cfg = get_system_config()
        cap = cfg.earning_cap
        total_earned = (
            Wallet.objects.filter(user_id=row.recipient_id)
            .values_list("total_earned", flat=True)
            .first()
        )
        if total_earned is None:
            total_earned = Decimal("0")
        if total_earned >= cap:
            return (
                f"Held: {recipient.full_name} has reached the earning cap of {cap}; "
                "further commissions cannot be credited."
            )
        return "Pending admin processing."

    # CREDITED
    order_number = order.order_number
    if row.commission_type == ct.DIRECT:
        note = (
            f"Direct sponsor commission for order {order_number} "
            f"from {row.source_user.member_id}."
        )
    elif row.commission_type == ct.UPLINE_L1:
        note = f"Passive upline L1 commission for order {order_number}."
    elif row.commission_type == ct.UPLINE_L2:
        note = f"Passive upline L2 commission for order {order_number}."
    elif row.commission_type == ct.UPLINE_L3:
        note = f"Passive upline L3 commission for order {order_number}."
    elif row.commission_type == ct.MILESTONE:
        note = "Milestone bonus credit."
    else:
        note = f"Commission credited for order {order_number}."

    if row.slot_band_held:
        note += (
            " Slot-band hold: counts toward total earnings only — not added to cash "
            "balance until band clears."
        )
    return note


def _tds_rate_percent(amount: Decimal, tds: Decimal) -> str | None:
    """Statutory Sec 194H rate from config (not misleading per-row effective %)."""
    if amount <= 0 or tds <= 0:
        return None
    cfg = get_system_config()
    try:
        pct = (cfg.tds_194h_rate or Decimal("0")) * Decimal("100")
        return str(pct.quantize(Decimal("0.01")))
    except Exception:
        return None


def serialize_admin_commission_row(row: CommissionLedger) -> dict[str, Any]:
    gross = row.amount
    tds = row.tds_deducted
    return {
        "id": row.id,
        "created_at": row.created_at.isoformat(),
        "order": {
            "id": row.order_id,
            "order_number": row.order.order_number,
            "razorpay_order_id": row.order.razorpay_order_id or None,
        },
        "earner": {
            "member_id": row.recipient.member_id,
            "full_name": row.recipient.full_name,
        },
        "buyer": {
            "member_id": row.source_user.member_id,
            "full_name": row.source_user.full_name,
        },
        "commission_type": row.commission_type,
        "level": commission_level_label(row.commission_type),
        "gross": str(gross),
        "tds_deducted": str(tds),
        "net_amount": str(row.net_amount),
        "tds_rate_percent": _tds_rate_percent(gross, tds),
        "status": row.status,
        "status_display": display_status_for_ledger(row),
        "wallet_reference": f"COMM-{row.order.order_number}",
    }


def serialize_admin_commission_detail(row: CommissionLedger) -> dict[str, Any]:
    base = serialize_admin_commission_row(row)
    base["note"] = _build_admin_commission_note(row)
    o = row.order
    base["order_detail"] = {
        "id": o.id,
        "order_number": o.order_number,
        "razorpay_order_id": o.razorpay_order_id or None,
        "status": o.status,
        "amount_paid": str(o.amount_paid),
        "paid_at": o.paid_at.isoformat() if o.paid_at else None,
        "is_retail_purchase": o.is_retail_purchase,
    }
    return base


def build_admin_commission_summary(flt: AdminCommissionFilters) -> dict[str, Any]:
    qs = apply_admin_commission_filters(base_ledger_queryset(), flt)
    st = CommissionLedger.Status
    paid = qs.filter(status=st.CREDITED).aggregate(s=Sum("net_amount"))["s"] or Decimal("0")
    pending = qs.filter(status__in=(st.PENDING, st.HELD)).aggregate(s=Sum("net_amount"))[
        "s"
    ] or Decimal("0")
    rev = qs.filter(status=st.REVERSED).aggregate(s=Sum("net_amount"))["s"] or Decimal("0")
    cfg = get_system_config()
    q2 = Decimal("0.01")
    return {
        "total_paid": str(paid.quantize(q2)),
        "pending": str(pending.quantize(q2)),
        "reversed": str(rev.quantize(q2)),
        "total_entries": qs.count(),
        "direct_commission_unit": str(cfg.direct_commission),
        "upline_commission_unit": str(cfg.upline_commission),
    }


def parse_pagination(query_params: dict[str, Any]) -> tuple[int, int]:
    try:
        page = max(1, int(query_params.get("page", 1) or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(query_params.get("page_size", DEFAULT_PAGE_SIZE) or DEFAULT_PAGE_SIZE)
    except (TypeError, ValueError):
        page_size = DEFAULT_PAGE_SIZE
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    return page, page_size
