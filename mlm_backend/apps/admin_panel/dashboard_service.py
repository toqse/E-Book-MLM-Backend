"""Aggregated payload for GET /api/v1/admin/dashboard/."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Exists, OuterRef, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.agreements.models import MemberComplianceProfile
from apps.commissions.models import CommissionLedger
from apps.finance.services.aggregates import paid_orders_qs, q2
from apps.finance.services.date_range import parse_finance_range
from apps.payments.models import Order
from apps.sponsor_slots.models import SponsorSlotAuditEvent, SponsorSlotCode
from apps.users.models import User
from apps.wallet.models import Wallet, WithdrawalRequest

ZERO = Decimal("0")


def pct_delta(current: Decimal | float | int, baseline: Decimal | float | int) -> float | None:
    try:
        b = Decimal(str(baseline))
        c = Decimal(str(current))
    except Exception:
        return None
    if b == 0:
        return None
    return float(((c - b) / b) * Decimal("100"))


def _parse_limit(raw: str | None, default: int, *, min_v: int = 1, max_v: int = 100) -> int:
    try:
        n = int(str(raw).strip())
    except Exception:
        n = default
    if n <= 0:
        n = default
    return max(min_v, min(n, max_v))


def _local_today() -> date:
    return timezone.localdate()


def _daterange(d0: date, d1: date) -> list[date]:
    out: list[date] = []
    d = d0
    while d <= d1:
        out.append(d)
        d += timedelta(days=1)
    return out


def _iso_week_key(d: date) -> tuple[int, int]:
    iso = d.isocalendar()
    return (iso.year, iso.week)


def _month_key(d: date) -> tuple[int, int]:
    return (d.year, d.month)


def _build_daily_series(
    d0: date, d1: date
) -> tuple[dict[date, Decimal], dict[date, Decimal]]:
    tz = timezone.get_current_timezone()

    book_qs = (
        paid_orders_qs(d0, d1)
        .annotate(day=TruncDate("paid_at", tzinfo=tz))
        .values("day")
        .annotate(s=Sum("amount_paid"))
    )
    book_by_day: dict[date, Decimal] = {}
    for row in book_qs:
        day = row["day"]
        if hasattr(day, "date"):
            day = day.date()
        book_by_day[day] = row["s"] or ZERO

    st = CommissionLedger.Status.CREDITED
    comm_qs = (
        CommissionLedger.objects.filter(
            status=st,
            created_at__date__gte=d0,
            created_at__date__lte=d1,
        )
        .annotate(day=TruncDate("created_at", tzinfo=tz))
        .values("day")
        .annotate(s=Sum("net_amount"))
    )
    comm_by_day: dict[date, Decimal] = {}
    for row in comm_qs:
        day = row["day"]
        if hasattr(day, "date"):
            day = day.date()
        comm_by_day[day] = row["s"] or ZERO

    return book_by_day, comm_by_day


def _rollup_series(
    book_by_day: dict[date, Decimal],
    comm_by_day: dict[date, Decimal],
    d0: date,
    d1: date,
    granularity: str,
) -> list[dict[str, Any]]:
    g = (granularity or "daily").strip().lower()
    if g not in ("daily", "weekly", "monthly"):
        g = "daily"

    daily_points: list[tuple[date, Decimal, Decimal]] = []
    for d in _daterange(d0, d1):
        b = book_by_day.get(d, ZERO)
        c = comm_by_day.get(d, ZERO)
        daily_points.append((d, b, c))

    if g == "daily":
        out: list[dict[str, Any]] = []
        for d, b, c in daily_points:
            comb = b + c
            out.append(
                {
                    "period_start": d.isoformat(),
                    "period_end": d.isoformat(),
                    "book_sales": str(q2(b)),
                    "commission_turnover": str(q2(c)),
                    "combined": str(q2(comb)),
                }
            )
        return out

    groups: dict[tuple[int, ...], list[tuple[date, Decimal, Decimal]]] = defaultdict(list)
    for d, b, c in daily_points:
        key: tuple[int, ...] = _iso_week_key(d) if g == "weekly" else _month_key(d)
        groups[key].append((d, b, c))

    out2: list[dict[str, Any]] = []
    for _k in sorted(groups.keys()):
        rows = groups[_k]
        rows.sort(key=lambda x: x[0])
        start_d = rows[0][0]
        end_d = rows[-1][0]
        tb = sum((x[1] for x in rows), ZERO)
        tc = sum((x[2] for x in rows), ZERO)
        comb = tb + tc
        out2.append(
            {
                "period_start": start_d.isoformat(),
                "period_end": end_d.isoformat(),
                "book_sales": str(q2(tb)),
                "commission_turnover": str(q2(tc)),
                "combined": str(q2(comb)),
            }
        )
    return out2


def _summary_cards(now) -> list[dict[str, Any]]:
    today = _local_today()
    yesterday = today - timedelta(days=1)
    member_qs = User.objects.filter(role=User.Role.MEMBER)

    total_members = member_qs.count()
    baseline_members = member_qs.filter(created_at__date__lte=today - timedelta(days=30)).count()
    dm_members = pct_delta(total_members, baseline_members) if baseline_members else None

    # "Active Today" = members who joined today AND have at least one paid
    # book order on the same calendar day (yesterday baseline mirrors this rule).
    paid_today_exists = Order.objects.filter(
        user_id=OuterRef("pk"),
        status=Order.Status.PAID,
        paid_at__date=today,
    )
    paid_yesterday_exists = Order.objects.filter(
        user_id=OuterRef("pk"),
        status=Order.Status.PAID,
        paid_at__date=yesterday,
    )
    active_today = (
        member_qs.filter(created_at__date=today)
        .filter(Exists(paid_today_exists))
        .count()
    )
    active_yesterday = (
        member_qs.filter(created_at__date=yesterday)
        .filter(Exists(paid_yesterday_exists))
        .count()
    )
    dm_active = pct_delta(active_today, active_yesterday) if active_yesterday else None

    cut24 = now - timedelta(hours=24)
    cut48 = now - timedelta(hours=48)
    sales_24h = Order.objects.filter(status=Order.Status.PAID, paid_at__gte=cut24).count()
    sales_prior_24h = Order.objects.filter(
        status=Order.Status.PAID,
        paid_at__gte=cut48,
        paid_at__lt=cut24,
    ).count()
    dm_sales = pct_delta(sales_24h, sales_prior_24h) if sales_prior_24h or sales_24h else None

    wr_open_statuses = (
        WithdrawalRequest.Status.PENDING,
        WithdrawalRequest.Status.APPROVED,
        WithdrawalRequest.Status.PROCESSING,
    )
    pending_sum = (
        WithdrawalRequest.objects.filter(status__in=wr_open_statuses).aggregate(s=Sum("net_payable"))["s"]
        or ZERO
    )
    week_ago = now - timedelta(days=7)
    two_weeks = now - timedelta(days=14)
    sum_recent_wr = (
        WithdrawalRequest.objects.filter(
            status__in=wr_open_statuses,
            created_at__gte=week_ago,
        ).aggregate(s=Sum("net_payable"))["s"]
        or ZERO
    )
    sum_prior_wr = (
        WithdrawalRequest.objects.filter(
            status__in=wr_open_statuses,
            created_at__gte=two_weeks,
            created_at__lt=week_ago,
        ).aggregate(s=Sum("net_payable"))["s"]
        or ZERO
    )
    dm_pending = pct_delta(sum_recent_wr, sum_prior_wr)

    now_dt = timezone.now()
    active_slots_total = SponsorSlotCode.objects.filter(
        status=SponsorSlotCode.Status.ACTIVE,
        expires_at__gt=now_dt,
    ).count()
    recent_slots = SponsorSlotCode.objects.filter(
        status=SponsorSlotCode.Status.ACTIVE,
        expires_at__gt=now_dt,
        created_at__gte=week_ago,
    ).count()
    baseline_slots = max(active_slots_total - recent_slots, 0)
    dm_slots = pct_delta(active_slots_total, baseline_slots) if baseline_slots > 0 else None

    compliance_exists = MemberComplianceProfile.objects.filter(user_id=OuterRef("pk"))
    verified_qs = member_qs.filter(Exists(compliance_exists)).filter(
        kyc_status=User.KYCStatus.VERIFIED,
    )
    score_now = round(100.0 * verified_qs.count() / total_members, 2) if total_members else 0.0
    cohort = member_qs.filter(created_at__date__lt=today - timedelta(days=30))
    cohort_total = cohort.count()
    score_cohort: float | None
    if cohort_total:
        cohort_verified = cohort.filter(Exists(compliance_exists)).filter(
            kyc_status=User.KYCStatus.VERIFIED,
        ).count()
        score_cohort = round(100.0 * cohort_verified / cohort_total, 2)
    else:
        score_cohort = None
    dm_comp = (
        pct_delta(Decimal(str(score_now)), Decimal(str(score_cohort)))
        if score_cohort is not None
        else None
    )

    return [
        {
            "id": "total_members",
            "label": "Total Members",
            "value": total_members,
            "delta_percent": dm_members,
            "compare_label": "vs_30d_ago_member_cohort",
            "unit": "count",
        },
        {
            "id": "active_today",
            "label": "Active Today",
            "value": active_today,
            "delta_percent": dm_active,
            "compare_label": "vs_yesterday",
            "unit": "count",
        },
        {
            "id": "book_sales_24h",
            "label": "Book Sales (24H)",
            "value": sales_24h,
            "delta_percent": dm_sales,
            "compare_label": "vs_prior_24h",
            "unit": "orders",
        },
        {
            "id": "pending_payouts",
            "label": "Pending Payouts",
            "value": str(q2(pending_sum)),
            "delta_percent": dm_pending,
            "compare_label": "vs_prior_7d_new_open_volume",
            "unit": "INR",
            "subtitle": "open_withdrawal_requests",
        },
        {
            "id": "active_slots",
            "label": "Active Slots",
            "value": active_slots_total,
            "delta_percent": dm_slots,
            "compare_label": "vs_older_active_pool",
            "unit": "count",
        },
        {
            "id": "compliance_score",
            "label": "Compliance Score",
            "value": int(round(score_now)),
            "delta_percent": dm_comp,
            "compare_label": "vs_30d_plus_member_cohort",
            "unit": "score_0_100",
        },
    ]


def _sponsor_slot_activity(limit: int) -> list[dict[str, Any]]:
    qs = SponsorSlotAuditEvent.objects.select_related("sponsor_slot_code", "actor").order_by(
        "-created_at", "-id"
    )[:limit]
    out: list[dict[str, Any]] = []
    for ev in qs:
        out.append(
            {
                "code": ev.sponsor_slot_code.code,
                "event_type": ev.event_type,
                "actor_member_id": ev.actor.member_id if ev.actor_id else None,
                "actor_full_name": ev.actor.full_name if ev.actor_id else None,
                "actor_is_system": ev.actor_id is None,
                "metadata": ev.metadata,
                "created_at": ev.created_at.isoformat(),
            }
        )
    return out


def _recent_joiners(limit: int) -> list[dict[str, Any]]:
    qs = (
        User.objects.filter(role=User.Role.MEMBER)
        .select_related("compliance_profile")
        .order_by("-created_at")[:limit]
    )
    out: list[dict[str, Any]] = []
    for u in qs:
        p = getattr(u, "compliance_profile", None)
        out.append(
            {
                "member_id": u.member_id,
                "full_name": u.full_name,
                "state": (p.state if p else "") or None,
                "kyc_status": u.kyc_status,
                "joined_at": u.created_at.date().isoformat(),
            }
        )
    return out


def _top_earners(limit: int) -> list[dict[str, Any]]:
    qs = (
        Wallet.objects.filter(user__role=User.Role.MEMBER)
        .select_related("user")
        .order_by("-total_earned", "-id")[:limit]
    )
    out: list[dict[str, Any]] = []
    for w in qs:
        u = w.user
        out.append(
            {
                "member_id": u.member_id,
                "full_name": u.full_name,
                "band": w.current_band,
                "referrals": u.direct_referral_count,
                "earnings": str(q2(w.total_earned)),
            }
        )
    return out


def build_admin_dashboard_payload(query_params: dict[str, Any] | None) -> dict[str, Any]:
    qp = query_params or {}
    now = timezone.now()
    today = _local_today()

    fr = parse_finance_range(qp)
    gran = (qp.get("revenue_granularity") or "daily").strip().lower()
    book_by_day, comm_by_day = _build_daily_series(fr.date_from, fr.date_to)
    points = _rollup_series(book_by_day, comm_by_day, fr.date_from, fr.date_to, gran)

    activity_limit = _parse_limit(qp.get("activity_limit"), 10, min_v=1, max_v=50)
    joiners_limit = _parse_limit(qp.get("recent_joiners_limit"), 10, min_v=1, max_v=50)
    earners_limit = _parse_limit(qp.get("top_earners_limit"), 10, min_v=1, max_v=50)

    pending_wr_count = WithdrawalRequest.objects.filter(
        status=WithdrawalRequest.Status.PENDING
    ).count()

    data: dict[str, Any] = {
        "summary_cards": _summary_cards(now),
        "revenue_series": {
            "granularity": gran if gran in ("daily", "weekly", "monthly") else "daily",
            "date_from": fr.date_from.isoformat(),
            "date_to": fr.date_to.isoformat(),
            "preset": fr.preset,
            "points": points,
        },
        "sponsor_slot_activity": _sponsor_slot_activity(activity_limit),
        "recent_joiners": _recent_joiners(joiners_limit),
        "top_earners": _top_earners(earners_limit),
        "total_members": User.objects.filter(role=User.Role.MEMBER).count(),
        "new_orders_today": Order.objects.filter(
            status=Order.Status.PAID,
            paid_at__date=today,
        ).count(),
        "pending_withdrawals": pending_wr_count,
        "pending_kyc": User.objects.filter(kyc_status=User.KYCStatus.PENDING).count(),
    }
    return data
