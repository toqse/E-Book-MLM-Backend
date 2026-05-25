from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.request import Request

from apps.admin_panel.utils import get_system_config
from apps.common.permissions import IsAdminRole
from apps.common.responses import envelope_response
from apps.commissions.milestone_tiers import MILESTONES
from apps.commissions.models import MilestoneRecord
from apps.tds.services import calculate_and_apply_194h_tds
from apps.users.models import User
from apps.wallet.bands import (
    SLOT_BAND_NUMBERS,
    _band_index_for_earnings,
    on_total_earned_updated,
)
from apps.wallet.models import Wallet, WalletTransaction

ZERO = Decimal("0")


def _parse_positive_int(raw, default: int, *, min_v: int = 1, max_v: int = 100) -> int:
    try:
        n = int(str(raw).strip())
    except Exception:
        n = default
    if n <= 0:
        n = default
    return max(min_v, min(n, max_v))


def _month_start(at: datetime | None = None) -> datetime:
    now = timezone.localtime(at or timezone.now())
    dt = datetime(now.year, now.month, 1, 0, 0, 0)
    if timezone.is_aware(now):
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _tier_label_for_threshold(threshold: int) -> str | None:
    for idx, (th, _pct, _bonus) in enumerate(MILESTONES, start=1):
        if int(th) == int(threshold):
            return f"T{idx}"
    return None


def _next_target(count: int) -> int | None:
    c = int(count or 0)
    for th, _pct, _bonus in MILESTONES:
        th_i = int(th)
        if c < th_i:
            return th_i
    return None


@dataclass(frozen=True)
class _ProcessResult:
    ok: bool
    reason: str | None = None


def _process_milestone_record_locked(*, record: MilestoneRecord, actor: User) -> _ProcessResult:
    """
    Called inside an atomic transaction with row locks.
    Credits wallet + creates WalletTransaction + marks milestone CREDITED.
    """
    if record.status != "PENDING":
        return _ProcessResult(False, "not_pending")

    user = record.user
    if user.kyc_status != User.KYCStatus.VERIFIED:
        record.status = "HELD"
        record.save(update_fields=["status"])
        return _ProcessResult(False, "kyc_not_verified")

    cfg = get_system_config()
    wallet, _ = Wallet.objects.select_for_update().get_or_create(user=user)
    remaining = (cfg.earning_cap or ZERO) - (wallet.total_earned or ZERO)
    if remaining <= 0:
        return _ProcessResult(False, "cap_reached")

    gross_pay = min(record.bonus_amount or ZERO, remaining)
    if gross_pay <= 0:
        return _ProcessResult(False, "no_payable_amount")

    tds = calculate_and_apply_194h_tds(user=user, gross_amount=gross_pay)

    # Re-evaluate slot-band gating at the moment of admin approval — a record
    # created weeks ago may have been HELD/PENDING through several band changes.
    band_before_credit = _band_index_for_earnings(wallet.total_earned or ZERO)
    slot_band_held = band_before_credit in SLOT_BAND_NUMBERS

    if not slot_band_held:
        wallet.cash_balance = (wallet.cash_balance or ZERO) + tds.net_amount
    wallet.total_earned = (wallet.total_earned or ZERO) + tds.net_amount
    wallet.total_tds_deducted = (wallet.total_tds_deducted or ZERO) + tds.tds_amount
    wallet.save()

    record.bonus_amount = tds.gross_amount
    record.tds_deducted = tds.tds_amount
    record.net_bonus = tds.net_amount
    record.status = "CREDITED"
    record.slot_band_held = slot_band_held
    record.save(
        update_fields=[
            "bonus_amount",
            "tds_deducted",
            "net_bonus",
            "status",
            "slot_band_held",
        ]
    )

    if not slot_band_held:
        WalletTransaction.objects.create(
            user=user,
            tx_type=WalletTransaction.TxType.CREDIT,
            amount=tds.net_amount,
            balance_after=wallet.cash_balance,
            reference=f"MILESTONE-{record.milestone_referrals}",
            meta={
                "type": "MILESTONE",
                "gross": str(tds.gross_amount),
                "tds": str(tds.tds_amount),
                "tds_rate_percent": str(tds.tds_rate_percent),
                "financial_year": tds.financial_year,
                "processed_by_admin_id": actor.id,
            },
        )
    on_total_earned_updated(wallet)
    return _ProcessResult(True, None)


@api_view(["GET"])
@permission_classes([IsAdminRole])
def admin_milestones_dashboard(request: Request):
    cfg = get_system_config()
    month_start = _month_start()

    liability = (
        MilestoneRecord.objects.filter(status="PENDING", created_at__gte=month_start).aggregate(s=Sum("bonus_amount"))[
            "s"
        ]
        or ZERO
    )
    paid = (
        MilestoneRecord.objects.filter(status="CREDITED", created_at__gte=month_start).aggregate(s=Sum("net_bonus"))["s"]
        or ZERO
    )
    achievers = (
        MilestoneRecord.objects.filter(status="CREDITED", created_at__gte=month_start).aggregate(n=Count("user", distinct=True))[
            "n"
        ]
        or 0
    )

    tiers = [
        {
            "tier": f"T{idx}",
            "threshold": int(th),
            "bonus_gross": str(bonus),
        }
        for idx, (th, _pct, bonus) in enumerate(MILESTONES, start=1)
    ]

    tracker_page = _parse_positive_int(request.query_params.get("page"), 1, min_v=1, max_v=1_000_000)
    tracker_page_size = _parse_positive_int(request.query_params.get("page_size"), 10, min_v=1, max_v=100)

    tracker_base = User.objects.filter(role=User.Role.MEMBER)
    tracker_count = tracker_base.count()
    tracker_total_pages = (
        (tracker_count + tracker_page_size - 1) // tracker_page_size if tracker_count else 0
    )
    start = (tracker_page - 1) * tracker_page_size
    tracker_qs = (
        tracker_base.only("id", "member_id", "full_name", "direct_referral_count")
        .order_by("-direct_referral_count", "id")[start : start + tracker_page_size]
    )

    tracker_results = []
    for u in tracker_qs:
        current = int(getattr(u, "direct_referral_count", 0) or 0)
        nxt = _next_target(current)
        tracker_results.append(
            {
                "user_id": u.id,
                "member_id": u.member_id,
                "full_name": u.full_name,
                "current_referrals": current,
                "next_target": nxt,
            }
        )

    return envelope_response(
        {
            "config": {
                "auto_process_milestone_bonuses": bool(getattr(cfg, "auto_process_milestone_bonuses", True)),
                "earning_cap": str(cfg.earning_cap),
                "tds_194r_rate": str(cfg.tds_194r_rate),
                "tds_cash_trigger": str(cfg.tds_cash_trigger),
            },
            "cards": {
                "monthly_liability_gross": str(liability),
                "paid_this_month_net": str(paid),
                "achievers_this_month": int(achievers),
            },
            "tiers": tiers,
            "member_progress_tracker": {
                "results": tracker_results,
                "count": tracker_count,
                "page": tracker_page,
                "page_size": tracker_page_size,
                "total_pages": tracker_total_pages,
            },
        }
    )


@api_view(["GET"])
@permission_classes([IsAdminRole])
def admin_milestones_queue(request: Request):
    page = _parse_positive_int(request.query_params.get("page"), 1, min_v=1, max_v=1_000_000)
    page_size = _parse_positive_int(request.query_params.get("page_size"), 20, min_v=1, max_v=100)
    status = (request.query_params.get("status") or "PENDING").strip().upper() or "PENDING"
    milestone_referrals = request.query_params.get("milestone_referrals") or request.query_params.get("tier")
    q = (request.query_params.get("q") or "").strip()

    qs = MilestoneRecord.objects.select_related("user").order_by("-created_at", "-id")
    if status:
        qs = qs.filter(status=status)
    if milestone_referrals not in (None, ""):
        try:
            mr = int(str(milestone_referrals).strip())
        except Exception:
            mr = None
        if mr is not None:
            qs = qs.filter(milestone_referrals=mr)
    if q:
        qf = Q(user__full_name__icontains=q) | Q(user__member_id__icontains=q)
        if q.isdigit():
            qi = int(q)
            qf = qf | Q(user_id=qi) | Q(id=qi)
        qs = qs.filter(qf)

    total_count = qs.count()
    total_pages = (total_count + page_size - 1) // page_size if total_count else 0
    start = (page - 1) * page_size
    rows = list(qs[start : start + page_size])

    results: list[dict[str, Any]] = []
    for r in rows:
        u = r.user
        results.append(
            {
                "record_id": r.id,
                "user_id": u.id,
                "member_id": u.member_id,
                "full_name": u.full_name,
                "tier_threshold": int(r.milestone_referrals),
                "tier": _tier_label_for_threshold(int(r.milestone_referrals)),
                "achieved_at": r.created_at.isoformat(),
                "bonus_gross": str(r.bonus_amount or ZERO),
                "status": r.status,
            }
        )

    return envelope_response(
        {
            "results": results,
            "count": total_count,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }
    )


@api_view(["POST"])
@permission_classes([IsAdminRole])
def admin_milestones_queue_process_one(request: Request, record_id: int):
    with transaction.atomic():
        rec = (
            MilestoneRecord.objects.select_for_update()
            .select_related("user")
            .filter(id=record_id)
            .first()
        )
        if not rec:
            return envelope_response(None, message="Not found", success=False, status=404)
        res = _process_milestone_record_locked(record=rec, actor=request.user)
        if not res.ok:
            return envelope_response(
                {"processed_ids": [], "skipped": [{"id": rec.id, "reason": res.reason}]},
                message="Not processed",
                success=False,
                errors={"detail": res.reason},
                status=400,
            )
        return envelope_response({"processed_ids": [rec.id], "skipped": []})


@api_view(["POST"])
@permission_classes([IsAdminRole])
def admin_milestones_queue_process_bulk(request: Request):
    raw_ids = request.data.get("record_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        return envelope_response(
            None,
            message="record_ids is required",
            success=False,
            errors={"record_ids": "required"},
            status=400,
        )
    ids: list[int] = []
    for x in raw_ids:
        try:
            ids.append(int(x))
        except Exception:
            continue
    # Preserve order but de-dup
    ids = [i for i in dict.fromkeys(ids) if i > 0]
    if not ids:
        return envelope_response(
            None,
            message="No valid record_ids provided",
            success=False,
            errors={"record_ids": "invalid"},
            status=400,
        )

    processed: list[int] = []
    skipped: list[dict[str, Any]] = []
    with transaction.atomic():
        recs = (
            MilestoneRecord.objects.select_for_update()
            .select_related("user")
            .filter(id__in=ids)
        )
        rec_by_id = {r.id: r for r in recs}
        for rid in ids:
            rec = rec_by_id.get(rid)
            if not rec:
                skipped.append({"id": rid, "reason": "not_found"})
                continue
            res = _process_milestone_record_locked(record=rec, actor=request.user)
            if not res.ok:
                skipped.append({"id": rid, "reason": res.reason})
                continue
            processed.append(rid)

    msg = "Processed" if not skipped else "Processed with some failures"
    return envelope_response({"processed_ids": processed, "skipped": skipped}, message=msg, success=not skipped)

