from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.admin_panel.utils import get_system_config
from apps.commissions.milestone_tiers import get_milestones
from apps.commissions.models import MilestoneRecord
from apps.users.models import User
from apps.wallet.models import Wallet

ZERO = Decimal("0")
QUALIFYING_REFERRALS_DEFINITION = (
    "Counted when a direct referral completes a qualifying MLM purchase processed by the "
    "commission engine (non-retail, first paid order unless repurchase commissions are enabled); "
    "buyer must be placed in the binary tree. Product price, KYC, and cap rules apply per platform config."
)


def _fy_start() -> datetime:
    today = timezone.localdate()
    year = today.year if today.month >= 4 else today.year - 1
    dt = datetime(year, 4, 1, 0, 0, 0)
    if settings.USE_TZ:
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _money(v: Decimal | int | float | None) -> str:
    if v is None:
        return str(ZERO)
    if not isinstance(v, Decimal):
        v = Decimal(str(v))
    q = v.quantize(Decimal("0.01"))
    return format(q, "f")


def build_user_milestones_dashboard(user: User) -> dict[str, Any]:
    cfg = get_system_config()
    milestones = get_milestones(cfg)
    wallet, _ = Wallet.objects.get_or_create(user_id=user.pk)
    count = int(getattr(user, "direct_referral_count", 0) or 0)

    records = list(MilestoneRecord.objects.filter(user=user).order_by("milestone_referrals"))
    by_threshold: dict[int, MilestoneRecord] = {r.milestone_referrals: r for r in records}

    thresholds = [int(t[0]) for t in milestones]

    total_tiers = len(milestones)
    milestones_completed = sum(
        1
        for th in thresholds
        if th in by_threshold and by_threshold[th].status == "CREDITED"
    )

    earned_net = sum(
        ((r.net_bonus or ZERO) for r in records if r.status == "CREDITED"),
        ZERO,
    )
    tds_sum = sum(((r.tds_deducted or ZERO) for r in records), ZERO)

    remaining_potential = ZERO
    for th, _, bonus in milestones:
        th = int(th)
        if th not in by_threshold:
            remaining_potential += bonus

    fy_start = _fy_start()
    fy_gross = sum(
        (
            (r.bonus_amount or ZERO)
            for r in records
            if r.status == "CREDITED" and r.created_at >= fy_start
        ),
        ZERO,
    )

    tds_rate_pct = (cfg.tds_194r_rate or ZERO) * Decimal("100")

    # Smallest threshold not yet paid and count still below threshold → IN_PROGRESS
    in_progress_threshold: int | None = None
    for th, _, _bonus in milestones:
        th = int(th)
        if th in by_threshold:
            continue
        if count < th:
            in_progress_threshold = th
            break

    milestone_bonus_sum_gross = sum(bonus for _th, _p, bonus in milestones)
    cap = cfg.earning_cap or ZERO
    cap_pct = (
        float((milestone_bonus_sum_gross / cap) * 100) if cap > 0 else 0.0
    )

    tiers_out: list[dict[str, Any]] = []
    for idx, (th, _pct, bonus_gross) in enumerate(milestones, start=1):
        th = int(th)
        rec = by_threshold.get(th)
        bonus_gross_dec = bonus_gross

        if rec is not None:
            status = "UNLOCKED"
            reason = None
        elif count >= th:
            status = "MISSED_OR_BLOCKED"
            reason = "no_record_at_threshold"
        elif th == in_progress_threshold:
            status = "IN_PROGRESS"
            reason = None
        else:
            status = "LOCKED"
            reason = None

        pct_toward = 0
        if th > 0:
            pct_toward = min(100, int(round(Decimal(100) * Decimal(count) / Decimal(th))))

        tiers_out.append(
            {
                "tier": f"T{idx}",
                "index": idx,
                "threshold": th,
                "bonus_gross": _money(bonus_gross_dec),
                "status": status,
                "status_reason": reason,
                "progress": {
                    "current_referrals": count,
                    "target_referrals": th,
                    "percent_toward_tier": pct_toward,
                },
                "remaining_to_threshold": max(0, th - count),
                "amounts": {
                    "bonus_gross": _money(bonus_gross_dec),
                    "tds_deducted": _money(rec.tds_deducted if rec else None),
                    "net_credited": _money(rec.net_bonus if rec else None),
                },
                "earned_at": rec.created_at.isoformat() if rec else None,
                "record_id": rec.id if rec else None,
            }
        )

    history = []
    for x in records:
        row: dict[str, Any] = {
            "referrals": x.milestone_referrals,
            "bonus": str(x.net_bonus),
            "status": x.status,
            "bonus_gross": str(x.bonus_amount),
            "tds_deducted": str(x.tds_deducted or ZERO),
            "earned_at": x.created_at.isoformat(),
        }
        history.append(row)

    return {
        "user": {
            "member_id": user.member_id,
            "full_name": user.full_name,
            "referral_code": user.referral_code,
            "referral_link": user.referral_link,
        },
        "qualifying_referrals": {
            "count": count,
            "definition": QUALIFYING_REFERRALS_DEFINITION,
        },
        "config_snapshot": {
            "earning_cap": _money(cap),
            "product_base_price": _money(cfg.product_base_price),
            "tds_194r_rate_percent": _money(tds_rate_pct),
            "tds_cash_trigger": _money(cfg.tds_cash_trigger),
        },
        "summary": {
            "milestones_completed": {"current": milestones_completed, "total": total_tiers},
            "bonus_earned_so_far": _money(earned_net),
            "remaining_potential_bonus_gross": _money(remaining_potential),
            "tds": {
                "milestone_tds_deducted_total": _money(tds_sum),
                "fy_milestone_gross_total": _money(fy_gross),
                "section": "194R",
                "trigger_threshold": _money(cfg.tds_cash_trigger),
                "rate_percent": _money(tds_rate_pct),
                "note": "Milestone credits are subject to tax withholding rules per platform policy; see your ledger entries for actual TDS deducted.",
            },
        },
        "policy": {
            "all_or_nothing": True,
            "bonuses_count_toward_earning_cap": True,
            "tds_section_194r": (
                f"Section 194R may apply on cumulative milestone benefits over "
                f"{_money(cfg.tds_cash_trigger)} in a financial year (display uses SystemConfig "
                f"tds_cash_trigger). Rate shown from tds_194r_rate ({_money(tds_rate_pct)}%)."
            ),
        },
        "cap_context": {
            "limit": _money(cap),
            "used": _money(wallet.total_earned),
            "remaining": _money(max(ZERO, cap - (wallet.total_earned or ZERO))),
        },
        "cap_impact": {
            "milestone_bonus_sum_gross": _money(milestone_bonus_sum_gross),
            "milestone_bonus_sum_percent_of_cap": round(cap_pct, 2),
        },
        "tiers": tiers_out,
        "history": history,
    }
