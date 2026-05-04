"""
Aggregated earnings and payouts payloads for member dashboards.
Optimized for bounded query counts (aggregates, select_related, no N+1).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import connection
from django.db.models import Case, Count, F, Q, Sum, When
from django.db.models.fields import DecimalField, IntegerField
from django.utils import timezone

from apps.admin_panel.models import SystemConfig
from apps.admin_panel.utils import get_system_config
from apps.commissions.models import CommissionLedger, MilestoneRecord
from apps.sponsor_slots.models import SponsorSlotCode
from apps.users.models import User
from apps.wallet.bands import BAND_EDGES
from apps.wallet.models import Wallet, WalletTransaction, WithdrawalRequest

SLOT_BAND_NUMBERS = frozenset({2, 4, 6, 8})
# Display value per redeemed sponsor code (issuer); aligns with common UI copy.
SLOT_LEDGER_UNIT_VALUE = Decimal("100")
ZERO = Decimal("0")


def _fy_start() -> datetime:
    """India FY start April 1 (current FY)."""
    today = timezone.localdate()
    year = today.year if today.month >= 4 else today.year - 1
    dt = datetime(year, 4, 1, 0, 0, 0)
    if settings.USE_TZ:
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _period_start(period: str) -> datetime | None:
    now = timezone.now()
    p = (period or "all").strip().lower()
    if p == "all":
        return None
    if p == "today":
        return datetime.combine(now.date(), datetime.min.time(), tzinfo=now.tzinfo)
    if p == "7d":
        return now - timedelta(days=7)
    if p == "30d":
        return now - timedelta(days=30)
    if p in ("fy", "fy_2025_26"):
        return _fy_start()
    return None


def _parse_include(raw: str | None) -> set[str]:
    s = (raw or "overview").strip().lower()
    parts = {x.strip() for x in s.split(",") if x.strip()}
    if "all" in parts:
        return {"overview", "ledger"}
    out: set[str] = set()
    if "overview" in parts or not parts:
        out.add("overview")
    if "ledger" in parts:
        out.add("ledger")
    if not out:
        out.add("overview")
    return out


def _parse_ledger_type(raw: str | None) -> str:
    t = (raw or "all").strip().lower()
    allowed = {"all", "direct", "passive", "milestone", "reversed", "pending"}
    return t if t in allowed else "all"


def get_wallet_row(user: User) -> Wallet:
    w, _ = Wallet.objects.get_or_create(user_id=user.pk)
    return w


def commission_aggregates_for_user(user_id: int) -> dict[str, Any]:
    """Single aggregate query for commission-type nets and units."""
    ct = CommissionLedger.CommissionType
    st = CommissionLedger.Status
    passive_types = (ct.UPLINE_L2, ct.UPLINE_L3, ct.UPLINE_L4)
    qs = CommissionLedger.objects.filter(recipient_id=user_id)
    dec = DecimalField(max_digits=12, decimal_places=2)
    int_out = IntegerField()
    return qs.aggregate(
        direct_net=Sum(
            Case(
                When(
                    Q(commission_type=ct.DIRECT) & Q(status=st.CREDITED),
                    then=F("net_amount"),
                ),
                default=ZERO,
                output_field=dec,
            )
        ),
        passive_net=Sum(
            Case(
                When(
                    Q(commission_type__in=passive_types) & Q(status=st.CREDITED),
                    then=F("net_amount"),
                ),
                default=ZERO,
                output_field=dec,
            )
        ),
        reversed_net=Sum(
            Case(
                When(status=st.REVERSED, then=F("net_amount")),
                default=ZERO,
                output_field=dec,
            )
        ),
        direct_units=Count(
            Case(
                When(Q(commission_type=ct.DIRECT) & Q(status=st.CREDITED), then=1),
                output_field=int_out,
            )
        ),
        passive_units=Count(
            Case(
                When(Q(commission_type__in=passive_types) & Q(status=st.CREDITED), then=1),
                output_field=int_out,
            )
        ),
    )


def refund_window_hold_net(user_id: int) -> Decimal:
    now = timezone.now()
    s = (
        CommissionLedger.objects.filter(
            recipient_id=user_id,
            status=CommissionLedger.Status.CREDITED,
            order__refund_eligible_until__gt=now,
        ).aggregate(s=Sum("net_amount"))["s"]
    )
    return s or ZERO


def milestone_net_total(user_id: int) -> Decimal:
    s = MilestoneRecord.objects.filter(user_id=user_id, status="CREDITED").aggregate(s=Sum("net_bonus"))["s"]
    return s or ZERO


def slot_issuer_display_total(user_id: int) -> tuple[Decimal, int]:
    n = SponsorSlotCode.objects.filter(
        issued_to_id=user_id,
        status=SponsorSlotCode.Status.REDEEMED,
    ).count()
    return (Decimal(n) * SLOT_LEDGER_UNIT_VALUE, n)


def build_commissions_summary(user: User, cfg: SystemConfig, wallet: Wallet) -> dict[str, str]:
    """Payload compatible with legacy GET /user/commissions/summary/."""
    agg = commission_aggregates_for_user(user.pk)
    ms = milestone_net_total(user.pk)
    passive = agg["passive_net"] or ZERO
    return {
        "direct": str(agg["direct_net"] or ZERO),
        "upline": str(passive),
        "milestone": str(ms),
        "tree_passive": str(passive),
    }


def build_overview(user: User, wallet: Wallet, cfg: SystemConfig) -> dict[str, Any]:
    agg = commission_aggregates_for_user(user.pk)
    ms_total = milestone_net_total(user.pk)
    slot_amt, slot_n = slot_issuer_display_total(user.pk)
    hold = refund_window_hold_net(user.pk)
    direct = agg["direct_net"] or ZERO
    passive = agg["passive_net"] or ZERO
    rev = agg["reversed_net"] or ZERO
    cap = cfg.earning_cap
    used = wallet.total_earned
    pct = float((used / cap) * 100) if cap and cap > 0 else 0.0
    kyc_ok = user.kyc_status == User.KYCStatus.VERIFIED
    return {
        "lifetime_total": str(used),
        "breakdown": {
            "direct": {
                "amount": str(direct),
                "units": int(agg["direct_units"] or 0),
                "unit_amount": str(cfg.direct_commission),
            },
            "passive": {
                "amount": str(passive),
                "units": int(agg["passive_units"] or 0),
                "unit_amount": str(cfg.upline_commission),
                "levels": "L2-L4",
            },
            "milestone": {"amount": str(ms_total), "units": None, "unit_amount": None},
            "slots": {
                "amount": str(slot_amt),
                "codes_redeemed": slot_n,
                "unit_amount": str(SLOT_LEDGER_UNIT_VALUE),
            },
        },
        "wallet": {
            "cash_balance": str(wallet.cash_balance),
            "total_earned": str(wallet.total_earned),
            "total_withdrawn": str(wallet.total_withdrawn),
            "on_hold": str(hold),
            "tds_fy": str(wallet.total_tds_deducted),
            "reversed": str(rev),
        },
        "cap": {
            "limit": str(cap),
            "used": str(used),
            "used_percent": round(pct, 2),
            "remaining": str(max(ZERO, cap - used)),
        },
        "kyc": {
            "status": user.kyc_status,
            "withdrawals_blocked": not kyc_ok,
        },
        "rates": {
            "direct_commission": str(cfg.direct_commission),
            "upline_commission": str(cfg.upline_commission),
            "earning_cap": str(cap),
            "is_repurchase_commission_allowed": bool(cfg.is_repurchase_commission_allowed),
        },
        "fy_label": wallet.fy_label,
    }


def _commission_level_label(ctype: str) -> str | None:
    if ctype == CommissionLedger.CommissionType.DIRECT:
        return "L1"
    mapping = {
        CommissionLedger.CommissionType.UPLINE_L2: "L2",
        CommissionLedger.CommissionType.UPLINE_L3: "L3",
        CommissionLedger.CommissionType.UPLINE_L4: "L4",
    }
    return mapping.get(ctype)


def _commission_display_type(row: CommissionLedger) -> str:
    if row.status == CommissionLedger.Status.REVERSED:
        return "Reversed"
    if row.status == CommissionLedger.Status.PENDING:
        return "Pending"
    if row.commission_type == CommissionLedger.CommissionType.DIRECT:
        return "Direct"
    if row.commission_type in (
        CommissionLedger.CommissionType.UPLINE_L2,
        CommissionLedger.CommissionType.UPLINE_L3,
        CommissionLedger.CommissionType.UPLINE_L4,
    ):
        return "Passive"
    return row.commission_type


def _commission_description(row: CommissionLedger) -> str:
    src = row.source_user.full_name if row.source_user_id else "Member"
    if row.status == CommissionLedger.Status.REVERSED:
        return f"Referral commission reversed — {src}"
    if row.commission_type == CommissionLedger.CommissionType.DIRECT:
        return f"Direct commission — {src} joined"
    if row.commission_type in (
        CommissionLedger.CommissionType.UPLINE_L2,
        CommissionLedger.CommissionType.UPLINE_L3,
        CommissionLedger.CommissionType.UPLINE_L4,
    ):
        mid = row.source_user.member_id if row.source_user_id else ""
        return f"Tree passive — {src} ({mid})".strip()
    return f"Commission — {row.get_commission_type_display()}"


def _serialize_commission(row: CommissionLedger) -> dict[str, Any]:
    src = row.source_user
    triggered = None
    if src:
        triggered = {"member_id": src.member_id, "full_name": src.full_name}
    return {
        "id": row.id,
        "kind": "COMMISSION",
        "at": row.created_at.isoformat(),
        "display_type": _commission_display_type(row),
        "level": _commission_level_label(row.commission_type),
        "description": _commission_description(row),
        "triggered_by": triggered,
        "via_downline": None,
        "gross": str(row.amount),
        "tds": str(row.tds_deducted),
        "net": str(row.net_amount),
        "status": row.status,
        "order_id": row.order_id,
    }


def _serialize_milestone(row: MilestoneRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": "MILESTONE",
        "at": row.created_at.isoformat(),
        "display_type": "Milestone",
        "level": None,
        "description": f"Milestone bonus — {row.milestone_referrals} direct referrals",
        "triggered_by": None,
        "via_downline": None,
        "gross": str(row.bonus_amount),
        "tds": str(row.tds_deducted),
        "net": str(row.net_bonus),
        "status": row.status,
        "order_id": None,
    }


def _ledger_type_sql_commission(typ: str) -> tuple[str, list[Any]]:
    """Returns SQL fragment for commissions_ledger WHERE (empty = no filter)."""
    st = CommissionLedger.Status
    ct = CommissionLedger.CommissionType
    if typ == "milestone":
        return " AND 1=0 ", []
    if typ == "direct":
        return " AND commission_type = %s AND status = %s", [ct.DIRECT, st.CREDITED]
    if typ == "passive":
        return (
            " AND commission_type IN (%s,%s,%s) AND status = %s",
            [ct.UPLINE_L2, ct.UPLINE_L3, ct.UPLINE_L4, st.CREDITED],
        )
    if typ == "reversed":
        return " AND status = %s", [st.REVERSED]
    if typ == "pending":
        return " AND status = %s", [st.PENDING]
    return "", []


def _ledger_type_sql_milestone(typ: str) -> tuple[str, list[Any]]:
    if typ in ("direct", "passive", "reversed"):
        return " AND 1=0 ", []
    if typ == "pending":
        return " AND status = %s", ["PENDING"]
    if typ == "milestone":
        return "", []
    return "", []


def build_ledger(
    user: User,
    *,
    period: str,
    typ: str,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    offset = (page - 1) * page_size
    since = _period_start(period)
    t = _parse_ledger_type(typ)

    cl_table = CommissionLedger._meta.db_table
    ms_table = MilestoneRecord._meta.db_table

    params: list[Any] = []
    c_where = f"recipient_id = {user.pk}"
    if since:
        c_where += " AND created_at >= %s"
        params.append(since)
    frag_c, extra_c = _ledger_type_sql_commission(t)
    c_where += frag_c
    params.extend(extra_c)

    m_where = f"user_id = {user.pk}"
    if since:
        m_where += " AND created_at >= %s"
        params.append(since)
    frag_m, extra_m = _ledger_type_sql_milestone(t)
    m_where += frag_m
    params.extend(extra_m)

    count_sql = f"""
        SELECT COUNT(*) FROM (
            SELECT id FROM {cl_table} WHERE {c_where}
            UNION ALL
            SELECT id FROM {ms_table} WHERE {m_where}
        ) AS _cnt
    """
    union_sql = f"""
        SELECT src, rid, created_at FROM (
            SELECT 'c' AS src, id AS rid, created_at
            FROM {cl_table} WHERE {c_where}
            UNION ALL
            SELECT 'm' AS src, id AS rid, created_at
            FROM {ms_table} WHERE {m_where}
        ) AS _u
        ORDER BY created_at DESC, rid DESC
        LIMIT %s OFFSET %s
    """

    with connection.cursor() as cursor:
        cursor.execute(count_sql, params)
        total_count = cursor.fetchone()[0]
        cursor.execute(union_sql, params + [page_size, offset])
        keys = cursor.fetchall()

    c_ids = [rid for src, rid, _ in keys if src == "c"]
    m_ids = [rid for src, rid, _ in keys if src == "m"]

    comm_by_id = {
        x.id: x
        for x in CommissionLedger.objects.filter(id__in=c_ids).select_related("source_user", "order")
    }
    ms_by_id = {x.id: x for x in MilestoneRecord.objects.filter(id__in=m_ids)}

    results: list[dict[str, Any]] = []
    for src, rid, _ca in keys:
        if src == "c":
            row = comm_by_id.get(rid)
            if row:
                results.append(_serialize_commission(row))
        else:
            row = ms_by_id.get(rid)
            if row:
                results.append(_serialize_milestone(row))

    return {
        "results": results,
        "total_count": total_count,
        "page": page,
        "page_size": page_size,
    }


def _band_range_display(idx_zero: int) -> tuple[str, str | None]:
    low = BAND_EDGES[idx_zero]
    if idx_zero + 1 < len(BAND_EDGES):
        high = BAND_EDGES[idx_zero + 1]
        return str(low), str(high)
    return str(low), None


def build_band_ladder(wallet: Wallet, cfg: SystemConfig) -> list[dict[str, Any]]:
    earned = wallet.total_earned
    out: list[dict[str, Any]] = []
    for i in range(9):
        band_num = i + 1
        low_s, high_s = _band_range_display(i)
        kind = "SLOT" if band_num in SLOT_BAND_NUMBERS else "CASH"
        low_dec = BAND_EDGES[i]
        high_dec = BAND_EDGES[i + 1] if i + 1 < len(BAND_EDGES) else None
        progress = None
        if high_dec is not None and low_dec <= earned < high_dec:
            span = high_dec - low_dec
            if span > 0:
                progress = float(((earned - low_dec) / span) * 100)
        elif high_dec is None and earned >= low_dec and band_num == 9:
            progress = 100.0
        out.append(
            {
                "band": band_num,
                "range_low": low_s,
                "range_high": high_s,
                "kind": kind,
                "slot_expiry_days": cfg.sponsor_slot_expiry_days,
                "is_current": wallet.current_band == band_num,
                "unlocked": wallet.current_band >= band_num,
                "progress_in_band_percent": round(progress, 2) if progress is not None else None,
            }
        )
    return out


def build_payouts_bundle(user: User, *, include_movements: bool) -> dict[str, Any]:
    wallet = get_wallet_row(user)
    cfg = get_system_config()
    kyc_ok = user.kyc_status == User.KYCStatus.VERIFIED
    wd_qs = (
        WithdrawalRequest.objects.filter(user=user)
        .order_by("-id")[:50]
        .only(
            "id",
            "band",
            "amount_requested",
            "net_payable",
            "tds_amount",
            "tds_section",
            "status",
            "created_at",
            "updated_at",
        )
    )
    withdrawals = [
        {
            "id": x.id,
            "band": x.band,
            "amount_requested": str(x.amount_requested),
            "net_payable": str(x.net_payable),
            "tds_amount": str(x.tds_amount),
            "tds_section": x.tds_section,
            "status": x.status,
            "created_at": x.created_at.isoformat(),
            "updated_at": x.updated_at.isoformat(),
        }
        for x in wd_qs
    ]
    data: dict[str, Any] = {
        "wallet": {
            "cash_balance": str(wallet.cash_balance),
            "total_earned": str(wallet.total_earned),
            "total_withdrawn": str(wallet.total_withdrawn),
            "total_tds_deducted": str(wallet.total_tds_deducted),
            "fy_label": wallet.fy_label,
            "band_cash_withdrawn_fy": str(wallet.band_cash_withdrawn_fy),
            "current_band": wallet.current_band,
            "withdrawals_blocked": not kyc_ok,
            "kyc_status": user.kyc_status,
        },
        "bands": build_band_ladder(wallet, cfg),
        "withdrawals": withdrawals,
    }
    if include_movements:
        txs = (
            WalletTransaction.objects.filter(user=user)
            .order_by("-created_at")[:25]
            .only("tx_type", "amount", "balance_after", "reference", "created_at")
        )
        data["recent_movements"] = [
            {
                "type": x.tx_type,
                "amount": str(x.amount),
                "balance_after": str(x.balance_after),
                "reference": x.reference,
                "at": x.created_at.isoformat(),
            }
            for x in txs
        ]
    return data


def build_earnings_response(
    user: User,
    *,
    include_raw: str | None,
    period: str,
    ledger_type: str,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    wallet = get_wallet_row(user)
    cfg = get_system_config()
    include = _parse_include(include_raw)
    data: dict[str, Any] = {}
    if "overview" in include:
        data["overview"] = build_overview(user, wallet, cfg)
    if "ledger" in include:
        data["ledger"] = build_ledger(
            user,
            period=period,
            typ=ledger_type,
            page=page,
            page_size=page_size,
        )
    return data
