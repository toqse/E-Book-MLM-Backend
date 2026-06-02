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
from django.db.models.fields import DecimalField
from django.utils import timezone

from apps.admin_panel.models import SystemConfig
from apps.admin_panel.utils import get_system_config
from apps.commissions.models import CommissionLedger, MilestoneRecord
from apps.sponsor_slots.models import SponsorSlotCode
from apps.users.models import User
from apps.wallet.bands import BAND_EDGES, SLOT_BAND_NUMBERS
from apps.wallet.models import Wallet, WalletTransaction, WithdrawalRequest

ZERO = Decimal("0")


def _fmt_money(v: Decimal | None) -> str:
    return str(v or ZERO)


def _fmt_date_time(dt: datetime) -> tuple[str, str]:
    """Local calendar date and 12h time for ledger UI columns."""
    local_dt = timezone.localtime(dt) if timezone.is_aware(dt) else dt
    return local_dt.strftime("%d %b %Y"), local_dt.strftime("%I:%M %p")


def _ledger_status_label(status: str) -> str:
    """Title-case label for STATUS column (matches common UI chips)."""
    s = (status or "").strip().upper()
    if s == "CREDITED":
        return "Credited"
    if s == "REVERSED":
        return "Reversed"
    if s == "PENDING":
        return "Pending"
    if s == "HELD":
        return "Held"
    return s.title() or status


def cooling_snapshot(*, user: User, wallet: Wallet, cooling_days: int) -> dict[str, Any]:
    """
    Recent CREDIT movements within `cooling_days` are locked from withdrawal.
    Returns decimal values (not strings) for internal composition.
    """
    days = max(0, int(cooling_days))
    cutoff = timezone.now() - timedelta(days=days)
    recent_credit = (
        WalletTransaction.objects.filter(
            user=user,
            tx_type=WalletTransaction.TxType.CREDIT,
            created_at__gt=cutoff,
        ).aggregate(s=Sum("amount"))["s"]
        or ZERO
    )
    locked_balance = min(wallet.cash_balance or ZERO, recent_credit)
    available_balance = max(ZERO, (wallet.cash_balance or ZERO) - locked_balance)
    return {
        "cooling_days": days,
        "locked_balance": locked_balance,
        "available_balance": available_balance,
    }


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
    allowed = {
        "all",
        "direct",
        "passive",
        "milestone",
        "withdrawal",
    }
    return t if t in allowed else "all"


def get_wallet_row(user: User) -> Wallet:
    w, _ = Wallet.objects.get_or_create(user_id=user.pk)
    return w


_HELD_STATUSES = (
    WithdrawalRequest.Status.PENDING,
    WithdrawalRequest.Status.APPROVED,
    WithdrawalRequest.Status.PROCESSING,
    WithdrawalRequest.Status.FAILED,
)


def withdrawal_status_breakdown(user_id: int) -> dict[str, Decimal]:
    """
    Break down wallet withdrawals by request status.

    - already_paid_out: PAID withdrawals (net_payable)
    - held_for_review: PENDING/APPROVED/PROCESSING/FAILED withdrawals (net_payable)

    REJECTED is intentionally excluded because the money is already restored to the wallet.
    """
    dec = DecimalField(max_digits=12, decimal_places=2)
    agg = WithdrawalRequest.objects.filter(user_id=user_id).aggregate(
        already_paid_out=Sum(
            Case(
                When(status=WithdrawalRequest.Status.PAID, then=F("net_payable")),
                default=ZERO,
                output_field=dec,
            )
        ),
        held_for_review=Sum(
            Case(
                When(status__in=_HELD_STATUSES, then=F("net_payable")),
                default=ZERO,
                output_field=dec,
            )
        ),
    )
    return {
        "already_paid_out": agg.get("already_paid_out") or ZERO,
        "held_for_review": agg.get("held_for_review") or ZERO,
    }


def build_withdrawn_block(wallet: Wallet) -> dict[str, str]:
    """
    JSON payload for the member wallet "withdrawn" breakdown.

    total is Wallet.total_withdrawn to keep backward compatibility with existing accounting.
    """
    br = withdrawal_status_breakdown(wallet.user_id)
    return {
        "total": _fmt_money(wallet.total_withdrawn),
        "already_paid_out": _fmt_money(br["already_paid_out"]),
        "held_for_review": _fmt_money(br["held_for_review"]),
    }


def commission_aggregates_for_user(user_id: int) -> dict[str, Any]:
    """Single aggregate query for commission-type nets and units."""
    ct = CommissionLedger.CommissionType
    st = CommissionLedger.Status
    passive_types = (ct.UPLINE_L1, ct.UPLINE_L2, ct.UPLINE_L3)
    qs = CommissionLedger.objects.filter(recipient_id=user_id)
    dec = DecimalField(max_digits=12, decimal_places=2)
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
            "order_id",
            filter=Q(commission_type=ct.DIRECT) & Q(status=st.CREDITED),
            distinct=True,
        ),
        passive_units=Count(
            "order_id",
            filter=Q(commission_type__in=passive_types) & Q(status=st.CREDITED),
            distinct=True,
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


def slot_issuer_display_total(
    user_id: int, unit_value: Decimal | None = None
) -> tuple[Decimal, int]:
    """Aggregate the issuer-side display total for redeemed sponsor codes.

    `unit_value` is the per-code display amount, normally the live ebook base
    price (`SystemConfig.product_base_price`). Falls back to that config when
    not supplied so older callers keep working.
    """
    n = SponsorSlotCode.objects.filter(
        issued_to_id=user_id,
        status=SponsorSlotCode.Status.REDEEMED,
    ).count()
    if unit_value is None:
        cfg = get_system_config()
        unit_value = cfg.product_base_price or ZERO
    return (Decimal(n) * (unit_value or ZERO), n)


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
    slot_unit = cfg.product_base_price or ZERO
    slot_amt, slot_n = slot_issuer_display_total(user.pk, slot_unit)
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
                "levels": "L1-L3",
            },
            "milestone": {"amount": str(ms_total), "units": None, "unit_amount": None},
            "slots": {
                "amount": str(slot_amt),
                "codes_redeemed": slot_n,
                "unit_amount": str(slot_unit),
            },
        },
        "wallet": {
            "cash_balance": str(wallet.cash_balance),
            "total_earned": str(wallet.total_earned),
            "total_withdrawn": build_withdrawn_block(wallet),
            "on_hold": str(hold),
            "tds_fy": str(wallet.total_tds_deducted),
            "tds_payable_194r": str(wallet.tds_payable),
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


def _band_text(current_band: int) -> tuple[str, str]:
    if current_band >= 9:
        return "Band 9", "Top band reached"
    return f"Band {current_band}", f"Next: Band {current_band + 1}"


def build_ui_summary(user: User, wallet: Wallet, cfg: SystemConfig) -> dict[str, Any]:
    agg = commission_aggregates_for_user(user.pk)
    ms_total = milestone_net_total(user.pk)
    slot_unit = cfg.product_base_price or ZERO
    slot_amt, slot_n = slot_issuer_display_total(user.pk, slot_unit)
    direct = agg["direct_net"] or ZERO
    passive = agg["passive_net"] or ZERO
    cap = cfg.earning_cap or ZERO
    used = wallet.total_earned or ZERO
    used_pct = float((used / cap) * 100) if cap > 0 else 0.0
    remaining = max(ZERO, cap - used)
    band_name, next_band = _band_text(wallet.current_band)
    tds_rate = (cfg.tds_194h_rate or ZERO) * Decimal("100")
    cooling_days = max(0, int(cfg.cooling_off_days))
    cool = cooling_snapshot(user=user, wallet=wallet, cooling_days=cooling_days)
    available_balance = cool["available_balance"]
    locked_balance = cool["locked_balance"]
    band_policy = (
        f"Current withdrawal band — cash payout, TDS {tds_rate:.0f}% (Sec 194H); "
        f"trigger from {_fmt_money(cfg.tds_cash_trigger)}."
    )
    one_credit = (
        f"Direct L1 {_fmt_money(cfg.direct_commission)} or passive L1–L3 "
        f"{_fmt_money(cfg.upline_commission)} per qualifying join."
    )

    return {
        "title": "My Earnings",
        "subtitle": "Commissions, tree passive, milestones",
        "lifetime_earnings": _fmt_money(used),
        "cap": {
            "used_pct": round(used_pct, 2),
            "used": _fmt_money(used),
            "limit": _fmt_money(cap),
            "remaining": _fmt_money(remaining),
        },
        "wallet": {
            "available_to_withdraw": _fmt_money(available_balance),
            "locked": _fmt_money(locked_balance),
            "withdrawn": build_withdrawn_block(wallet),
            "tds_payable_194r": _fmt_money(wallet.tds_payable),
            "total_tds_deducted": _fmt_money(wallet.total_tds_deducted),
        },
        "income": {
            "direct_l1": {
                "amount": _fmt_money(direct),
                "units": int(agg["direct_units"] or 0),
                "unit": _fmt_money(cfg.direct_commission),
            },
            "passive_l1_l3": {
                "amount": _fmt_money(passive),
                "units": int(agg["passive_units"] or 0),
                "unit": _fmt_money(cfg.upline_commission),
            },
            "milestone": {"amount": _fmt_money(ms_total)},
            "slots": {
                "amount": _fmt_money(slot_amt),
                "redeemed": slot_n,
                "unit": _fmt_money(slot_unit),
            },
        },
        "band": {
            "label": band_name,
            "number": wallet.current_band,
            "next": next_band,
            "policy": band_policy,
        },
        "rule": {"title": "One-credit rule", "text": one_credit},
        "fy_label": wallet.fy_label,
        "kyc_status": user.kyc_status,
    }


def _commission_level_label(ctype: str) -> str | None:
    if ctype == CommissionLedger.CommissionType.DIRECT:
        return "L1"
    mapping = {
        CommissionLedger.CommissionType.UPLINE_L1: "L1",
        CommissionLedger.CommissionType.UPLINE_L2: "L2",
        CommissionLedger.CommissionType.UPLINE_L3: "L3",
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
        CommissionLedger.CommissionType.UPLINE_L1,
        CommissionLedger.CommissionType.UPLINE_L2,
        CommissionLedger.CommissionType.UPLINE_L3,
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
        CommissionLedger.CommissionType.UPLINE_L1,
        CommissionLedger.CommissionType.UPLINE_L2,
        CommissionLedger.CommissionType.UPLINE_L3,
    ):
        mid = row.source_user.member_id if row.source_user_id else ""
        return f"Tree passive — {src} ({mid})".strip()
    return f"Commission — {row.get_commission_type_display()}"


def _tds_withheld_description(*, base_desc: str, tds_amount: Decimal, rate_note: str) -> str:
    return f"TDS withheld (Sec {rate_note}) — {base_desc} — ₹{_fmt_money(tds_amount)}"


def _serialize_commission_entries(row: CommissionLedger) -> list[dict[str, Any]]:
    """
    Up to two ledger rows: gross credit + optional TDS withholding line.

    Cash bands (194H): TDS is debited from cash_balance, so the TDS row carries
    a negative balance delta.
    Slot bands (194R): TDS is accrued to tds_payable (settled later from a real
    WalletTransaction(TxType.TDS) row), so the virtual TDS line here carries a
    ZERO balance delta to avoid double-counting at settlement time.
    """
    tds_amt = row.tds_deducted or ZERO
    if tds_amt <= ZERO or row.status != CommissionLedger.Status.CREDITED:
        return [_serialize_commission(row)]

    gross = row.amount or ZERO
    base = _serialize_commission(row)
    is_slot = bool(row.slot_band_held)
    rate_note = "194R" if is_slot else "194H"
    credit_delta = ZERO if is_slot else gross
    credit = {
        **base,
        "gross": _fmt_money(gross),
        "tds": "0",
        "net": _fmt_money(gross),
        "net_credited": _fmt_money(gross),
        "tds_deducted": "0",
        "_balance_delta": credit_delta,
        "cash_credited": not is_slot,
    }
    tds_delta = ZERO if is_slot else -tds_amt
    tds_row = {
        **base,
        "id": -abs(row.id),
        "kind": "COMMISSION_TDS",
        "type": "TDS Withheld",
        "detail": _tds_withheld_description(
            base_desc=base["detail"],
            tds_amount=tds_amt,
            rate_note=rate_note,
        ),
        "description": _tds_withheld_description(
            base_desc=base["description"],
            tds_amount=tds_amt,
            rate_note=rate_note,
        ),
        "gross": _fmt_money(-tds_amt if not is_slot else ZERO),
        "tds": _fmt_money(tds_amt),
        "net": _fmt_money(-tds_amt if not is_slot else ZERO),
        "net_credited": _fmt_money(-tds_amt if not is_slot else ZERO),
        "tds_deducted": _fmt_money(tds_amt),
        "status_label": "TDS Accrued" if is_slot else "TDS Withheld",
        "tds_section": rate_note,
        "_balance_delta": tds_delta,
        "cash_credited": False,
    }
    return [credit, tds_row]


def _serialize_milestone_entries(row: MilestoneRecord) -> list[dict[str, Any]]:
    tds_amt = row.tds_deducted or ZERO
    if tds_amt <= ZERO or row.status != "CREDITED":
        return [_serialize_milestone(row)]

    gross = row.bonus_amount or ZERO
    base = _serialize_milestone(row)
    is_slot = bool(row.slot_band_held)
    rate_note = "194R" if is_slot else "194H"
    credit_delta = ZERO if is_slot else gross
    credit = {
        **base,
        "gross": _fmt_money(gross),
        "tds": "0",
        "net": _fmt_money(gross),
        "net_credited": _fmt_money(gross),
        "tds_deducted": "0",
        "_balance_delta": credit_delta,
        "cash_credited": not is_slot,
    }
    tds_delta = ZERO if is_slot else -tds_amt
    tds_row = {
        **base,
        "id": -abs(row.id),
        "kind": "MILESTONE_TDS",
        "type": "TDS Withheld",
        "detail": _tds_withheld_description(
            base_desc=base["detail"],
            tds_amount=tds_amt,
            rate_note=rate_note,
        ),
        "description": _tds_withheld_description(
            base_desc=base["description"],
            tds_amount=tds_amt,
            rate_note=rate_note,
        ),
        "gross": _fmt_money(-tds_amt if not is_slot else ZERO),
        "tds": _fmt_money(tds_amt),
        "net": _fmt_money(-tds_amt if not is_slot else ZERO),
        "net_credited": _fmt_money(-tds_amt if not is_slot else ZERO),
        "tds_deducted": _fmt_money(tds_amt),
        "status_label": "TDS Accrued" if is_slot else "TDS Withheld",
        "tds_section": rate_note,
        "_balance_delta": tds_delta,
        "cash_credited": False,
    }
    return [credit, tds_row]


def _serialize_tds_settlement(wt: WalletTransaction) -> dict[str, Any]:
    """Real WalletTransaction(TxType.TDS) settlement of accrued 194R TDS."""
    amt = wt.amount or ZERO
    date_s, time_s = _fmt_date_time(wt.created_at)
    neg = _fmt_money(-amt)
    section = (wt.meta or {}).get("section", "194R")
    desc = f"TDS settled (Sec {section}) — ₹{_fmt_money(amt)}"
    return {
        "id": wt.id,
        "kind": "TDS_SETTLEMENT",
        "at": wt.created_at.isoformat(),
        "date": date_s,
        "time": time_s,
        "type": "TDS Settled",
        "detail": desc,
        "description": desc,
        "triggered_by": None,
        "via_downline": None,
        "level": None,
        "gross": neg,
        "tds": _fmt_money(amt),
        "net": neg,
        "status": "SETTLED",
        "status_label": "TDS Settled",
        "tds_section": section,
        "_balance_delta": -amt,
        "cash_credited": False,
    }


def _serialize_commission(row: CommissionLedger) -> dict[str, Any]:
    src = row.source_user
    triggered = None
    if src:
        triggered = {"member_id": src.member_id, "full_name": src.full_name}
    net = row.net_amount or ZERO
    st = row.status
    held = bool(row.slot_band_held)
    if held:
        # Slot-band-held credits never touched cash_balance and their reversal
        # never decremented cash either, so the running-balance walk must stay
        # flat across them in both states.
        balance_delta = ZERO
    elif st == CommissionLedger.Status.CREDITED:
        balance_delta = net
    elif st == CommissionLedger.Status.REVERSED:
        balance_delta = -net
    else:
        balance_delta = ZERO
    level = _commission_level_label(row.commission_type)
    desc = _commission_description(row)
    if held:
        desc = f"{desc} (Slot fund)"
    date_s, time_s = _fmt_date_time(row.created_at)
    status_label = _ledger_status_label(row.status)
    if held and st == CommissionLedger.Status.CREDITED:
        status_label = f"{status_label} (Slot fund)"
    out: dict[str, Any] = {
        "id": row.id,
        "kind": "COMMISSION",
        "at": row.created_at.isoformat(),
        "date": date_s,
        "time": time_s,
        "type": _commission_display_type(row),
        "detail": desc,
        "description": desc,
        "triggered_by": triggered,
        "via_downline": None,
        "gross": _fmt_money(row.amount),
        "tds": _fmt_money(row.tds_deducted),
        "net": _fmt_money(net),
        "status": row.status,
        "status_label": status_label,
        "slot_band_held": held,
        "cash_credited": (st == CommissionLedger.Status.CREDITED) and not held,
        "_balance_delta": balance_delta,
    }
    if level is not None:
        out["level"] = level
    if triggered is not None:
        out["source"] = triggered
    if row.order_id is not None:
        out["order_id"] = row.order_id
    return out


def _milestone_display_type(status: str) -> str:
    if status == "CREDITED":
        return "Milestone"
    if status == "PENDING":
        return "Pending"
    if status == "HELD":
        return "Held"
    return "Milestone"


def _serialize_milestone(row: MilestoneRecord) -> dict[str, Any]:
    net = row.net_bonus or ZERO
    held = bool(row.slot_band_held)
    desc = f"Milestone bonus — {row.milestone_referrals} direct referrals"
    if held:
        desc = f"{desc} (Slot fund)"
    date_s, time_s = _fmt_date_time(row.created_at)
    status_label = _ledger_status_label(row.status)
    if held and row.status == "CREDITED":
        status_label = f"{status_label} (Slot fund)"
    if held:
        balance_delta = ZERO
    else:
        balance_delta = net if row.status == "CREDITED" else ZERO
    return {
        "id": row.id,
        "kind": "MILESTONE",
        "at": row.created_at.isoformat(),
        "date": date_s,
        "time": time_s,
        "type": _milestone_display_type(row.status),
        "detail": desc,
        "description": desc,
        "triggered_by": None,
        "via_downline": None,
        "level": None,
        "gross": _fmt_money(row.bonus_amount),
        "tds": _fmt_money(row.tds_deducted),
        "net": _fmt_money(net),
        "status": row.status,
        "status_label": status_label,
        "referrals": int(row.milestone_referrals),
        "slot_band_held": held,
        "cash_credited": (row.status == "CREDITED") and not held,
        "_balance_delta": balance_delta,
    }


def _withdrawal_id_from_transaction(wt: WalletTransaction) -> int | None:
    meta = wt.meta or {}
    wid = meta.get("withdrawal_id")
    if wid is not None:
        try:
            return int(wid)
        except (TypeError, ValueError):
            pass
    ref = (wt.reference or "").strip()
    if ":" in ref:
        try:
            return int(ref.split(":", 1)[1])
        except (TypeError, ValueError):
            pass
    return None


def _withdrawal_debit_type_status(wr: WithdrawalRequest | None) -> tuple[str, str, str]:
    """Display type, status_label, and status code for a withdrawal DEBIT row."""
    if wr is None:
        return "Withdrawal", "Withdrawal", "UNKNOWN"
    st = wr.status
    labels: dict[str, tuple[str, str]] = {
        WithdrawalRequest.Status.PENDING: ("Withdrawal", "Withdrawal Pending"),
        WithdrawalRequest.Status.APPROVED: ("Withdrawal", "Withdrawal Processing"),
        WithdrawalRequest.Status.PROCESSING: ("Withdrawal", "Withdrawal Processing"),
        WithdrawalRequest.Status.PAID: ("Withdrawal", "Withdrawal Paid"),
        WithdrawalRequest.Status.REJECTED: ("Withdrawal", "Withdrawal Rejected"),
        WithdrawalRequest.Status.FAILED: ("Withdrawal", "Withdrawal Failed"),
    }
    type_disp, status_label = labels.get(st, ("Withdrawal", "Withdrawal"))
    return type_disp, status_label, st


def _withdrawal_detail(
    wr: WithdrawalRequest | None,
    wt: WalletTransaction,
    *,
    is_refund: bool,
) -> str:
    if is_refund:
        return "Withdrawal refunded — amount returned to wallet"
    method = ""
    if wr:
        method = (wr.payout_method or "").strip()
    elif wt.meta:
        method = (str(wt.meta.get("payout_method") or "")).strip()
    amt = _fmt_money(wt.amount)
    method_part = f" — {method}" if method else ""
    base = f"Withdrawal{method_part} — {amt}"
    if wr:
        if wr.status == WithdrawalRequest.Status.PAID and wr.utr_number:
            return f"{base} — Paid (UTR {wr.utr_number})"
        if wr.status == WithdrawalRequest.Status.REJECTED:
            return f"{base} — Rejected"
    return base


def _serialize_withdrawal_debit(
    wt: WalletTransaction,
    wr: WithdrawalRequest | None,
) -> dict[str, Any]:
    amt = wt.amount or ZERO
    wr_id = _withdrawal_id_from_transaction(wt)
    type_disp, status_label, status = _withdrawal_debit_type_status(wr)
    desc = _withdrawal_detail(wr, wt, is_refund=False)
    date_s, time_s = _fmt_date_time(wt.created_at)
    neg = _fmt_money(-amt)
    out: dict[str, Any] = {
        "id": wt.id,
        "kind": "WITHDRAWAL",
        "at": wt.created_at.isoformat(),
        "date": date_s,
        "time": time_s,
        "type": type_disp,
        "detail": desc,
        "description": desc,
        "triggered_by": None,
        "via_downline": None,
        "level": None,
        "gross": neg,
        "tds": "0",
        "net": neg,
        "status": status,
        "status_label": status_label,
        "_balance_delta": -amt,
    }
    if wr_id is not None:
        out["withdrawal_id"] = wr_id
    if wr:
        out["payout_method"] = wr.payout_method
        if wr.utr_number:
            out["utr_number"] = wr.utr_number
    elif wt.meta and wt.meta.get("payout_method"):
        out["payout_method"] = wt.meta["payout_method"]
    return out


def _serialize_withdrawal_refund(
    wt: WalletTransaction,
    wr: WithdrawalRequest | None,
) -> dict[str, Any]:
    amt = wt.amount or ZERO
    wr_id = _withdrawal_id_from_transaction(wt)
    desc = _withdrawal_detail(wr, wt, is_refund=True)
    date_s, time_s = _fmt_date_time(wt.created_at)
    pos = _fmt_money(amt)
    out: dict[str, Any] = {
        "id": wt.id,
        "kind": "WITHDRAWAL_REFUND",
        "at": wt.created_at.isoformat(),
        "date": date_s,
        "time": time_s,
        "type": "Withdrawal Refunded",
        "detail": desc,
        "description": desc,
        "triggered_by": None,
        "via_downline": None,
        "level": None,
        "gross": pos,
        "tds": "0",
        "net": pos,
        "status": "REFUNDED",
        "status_label": "Refunded",
        "_balance_delta": amt,
    }
    if wr_id is not None:
        out["withdrawal_id"] = wr_id
    if wr:
        out["payout_method"] = wr.payout_method
    elif wt.meta and wt.meta.get("payout_method"):
        out["payout_method"] = wt.meta["payout_method"]
    return out


def _ledger_type_sql_commission(typ: str) -> tuple[str, list[Any]]:
    """Returns SQL fragment for commissions_ledger WHERE (empty = no filter)."""
    st = CommissionLedger.Status
    ct = CommissionLedger.CommissionType
    if typ == "tds":
        # Fetch rows that will produce a virtual TDS row in the serializer:
        # any CREDITED row with tds_deducted > 0 (covers 194H + 194R/slot).
        return " AND status = %s AND tds_deducted > 0", [st.CREDITED]
    if typ == "withdrawal":
        return " AND 1=0 ", []
    if typ == "milestone":
        return " AND 1=0 ", []
    if typ == "direct":
        return " AND commission_type = %s AND status = %s", [ct.DIRECT, st.CREDITED]
    if typ == "passive":
        return (
            " AND commission_type IN (%s,%s,%s) AND status = %s",
            [ct.UPLINE_L1, ct.UPLINE_L2, ct.UPLINE_L3, st.CREDITED],
        )
    if typ == "reversed":
        return " AND status = %s", [st.REVERSED]
    if typ == "pending":
        return " AND status = %s", [st.PENDING]
    return "", []


def _ledger_type_sql_milestone(typ: str) -> tuple[str, list[Any]]:
    if typ in ("direct", "passive", "reversed", "withdrawal"):
        return " AND 1=0 ", []
    if typ == "tds":
        return " AND status = %s AND tds_deducted > 0", ["CREDITED"]
    if typ == "pending":
        return " AND status = %s", ["PENDING"]
    if typ == "milestone":
        return "", []
    return "", []


def _ledger_wallet_where(user_pk: int, since: datetime | None, typ: str) -> tuple[str, list[Any]]:
    """
    Wallet rows surfaced in the earnings ledger union:
    - Withdrawal debits (tx_type=DEBIT, reference=withdrawal:%)
    - Withdrawal rejection refunds (tx_type=ADJUSTMENT, reference=withdrawal_reject:%)
    - Sec 194R TDS settlements (tx_type=TDS, reference=TDS-194R-SETTLE%)
    """
    debit = WalletTransaction.TxType.DEBIT
    adj = WalletTransaction.TxType.ADJUSTMENT
    tds = WalletTransaction.TxType.TDS

    if typ == "tds":
        w_where = f"user_id = {user_pk} AND tx_type = %s AND reference LIKE %s"
        params: list[Any] = [tds, "TDS-194R-SETTLE%"]
    elif typ == "withdrawal":
        w_where = (
            f"user_id = {user_pk} AND ("
            "(tx_type = %s AND reference LIKE %s) OR "
            "(tx_type = %s AND reference LIKE %s)"
            f")"
        )
        params = [debit, "withdrawal:%", adj, "withdrawal_reject:%"]
    elif typ in ("direct", "passive", "milestone", "reversed", "pending"):
        # These typed filters never include wallet movement rows.
        w_where = "1=0"
        params = []
    else:  # all
        w_where = (
            f"user_id = {user_pk} AND ("
            "(tx_type = %s AND reference LIKE %s) OR "
            "(tx_type = %s AND reference LIKE %s) OR "
            "(tx_type = %s AND reference LIKE %s)"
            f")"
        )
        params = [
            debit, "withdrawal:%",
            adj, "withdrawal_reject:%",
            tds, "TDS-194R-SETTLE%",
        ]

    if since:
        w_where += " AND created_at >= %s"
        params.append(since)
    return w_where, params


# Status tokens used to recognise "not-yet-credited" commission/milestone rows.
# Kept as plain strings so the SQL fragment can be reused across both tables
# (CommissionLedger and MilestoneRecord share the same status spelling).
_LEDGER_PRE_KYC_HIDDEN_STATUSES = ("HELD", "PENDING")


def _ledger_union_sql_and_params(
    user: User,
    *,
    period: str,
    typ: str,
) -> tuple[str, str, list[Any]]:
    """Returns (count_sql, union_sql_without_limit_offset, params for count/union)."""
    since = _period_start(period)
    t = _parse_ledger_type(typ)

    cl_table = CommissionLedger._meta.db_table
    ms_table = MilestoneRecord._meta.db_table
    wt_table = WalletTransaction._meta.db_table

    # Member-facing earnings ledger is scoped to activity from the moment the
    # user was first admin-approved (sticky `kyc_first_approved_at`). This
    # keeps pre-KYC HELD/PENDING placeholder rows — which never funded the
    # wallet — out of the response so members don't see "passive income"
    # lines that aren't actually theirs to spend.
    #
    # A pre-KYC row that an admin later releases via
    # `release_held_commissions_for_user` flips status to CREDITED while
    # keeping its original `created_at`; we deliberately let those resurface
    # (status escape clause) so the line that backs the wallet credit is
    # still visible.
    kyc_since = getattr(user, "kyc_first_approved_at", None)
    hidden_status_placeholders = ",".join(["%s"] * len(_LEDGER_PRE_KYC_HIDDEN_STATUSES))
    pre_kyc_status_clause = (
        f" AND (created_at >= %s OR status NOT IN ({hidden_status_placeholders}))"
    )

    params: list[Any] = []
    c_where = f"recipient_id = {user.pk}"
    if since:
        c_where += " AND created_at >= %s"
        params.append(since)
    if kyc_since:
        c_where += pre_kyc_status_clause
        params.append(kyc_since)
        params.extend(_LEDGER_PRE_KYC_HIDDEN_STATUSES)
    frag_c, extra_c = _ledger_type_sql_commission(t)
    c_where += frag_c
    params.extend(extra_c)

    m_where = f"user_id = {user.pk}"
    m_params: list[Any] = []
    if since:
        m_where += " AND created_at >= %s"
        m_params.append(since)
    if kyc_since:
        m_where += pre_kyc_status_clause
        m_params.append(kyc_since)
        m_params.extend(_LEDGER_PRE_KYC_HIDDEN_STATUSES)
    frag_m, extra_m = _ledger_type_sql_milestone(t)
    m_where += frag_m
    m_params.extend(extra_m)

    w_where, w_params = _ledger_wallet_where(user.pk, since, t)
    # Withdrawals can only happen after KYC verification, but apply the
    # same lower bound for consistency and as defence-in-depth against
    # any backfilled/imported rows that pre-date KYC approval.
    if kyc_since:
        w_where += " AND created_at >= %s"
        w_params.append(kyc_since)

    all_params = params + m_params + w_params

    count_sql = f"""
        SELECT COUNT(*) FROM (
            SELECT id FROM {cl_table} WHERE {c_where}
            UNION ALL
            SELECT id FROM {ms_table} WHERE {m_where}
            UNION ALL
            SELECT id FROM {wt_table} WHERE {w_where}
        ) AS _cnt
    """
    union_sql = f"""
        SELECT src, rid, created_at FROM (
            SELECT 'c' AS src, id AS rid, created_at
            FROM {cl_table} WHERE {c_where}
            UNION ALL
            SELECT 'm' AS src, id AS rid, created_at
            FROM {ms_table} WHERE {m_where}
            UNION ALL
            SELECT 'w' AS src, id AS rid, created_at
            FROM {wt_table} WHERE {w_where}
        ) AS _u
        ORDER BY created_at DESC, rid DESC
    """
    return count_sql, union_sql, all_params


def _hydrate_ledger_keys(
    user: User, keys: list[tuple], *, typ: str = "all"
) -> list[dict[str, Any]]:
    c_ids = [rid for src, rid, _ in keys if src == "c"]
    m_ids = [rid for src, rid, _ in keys if src == "m"]
    wt_ids = [rid for src, rid, _ in keys if src == "w"]

    comm_by_id = {
        x.id: x
        for x in CommissionLedger.objects.filter(id__in=c_ids).select_related("source_user", "order")
    }
    ms_by_id = {x.id: x for x in MilestoneRecord.objects.filter(id__in=m_ids)}
    wt_by_id = {x.id: x for x in WalletTransaction.objects.filter(id__in=wt_ids)}

    wr_ids = [
        wid
        for wt in wt_by_id.values()
        if (wid := _withdrawal_id_from_transaction(wt)) is not None
    ]
    wr_by_id = {x.id: x for x in WithdrawalRequest.objects.filter(id__in=wr_ids)}

    results: list[dict[str, Any]] = []
    for src, rid, _ca in keys:
        if src == "c":
            row = comm_by_id.get(rid)
            if row:
                if typ == "tds":
                    if (row.tds_deducted or ZERO) > ZERO and not row.slot_band_held:
                        results.extend(_serialize_commission_entries(row)[1:])
                    continue
                results.extend(_serialize_commission_entries(row))
        elif src == "m":
            row = ms_by_id.get(rid)
            if row:
                if typ == "tds":
                    if (row.tds_deducted or ZERO) > ZERO and not row.slot_band_held:
                        results.extend(_serialize_milestone_entries(row)[1:])
                    continue
                results.extend(_serialize_milestone_entries(row))
        else:
            wt = wt_by_id.get(rid)
            if not wt:
                continue
            if wt.tx_type == WalletTransaction.TxType.TDS:
                # 194R settlement: emitted regardless of typ since the WHERE
                # already restricts to TDS-194R-SETTLE% references.
                results.append(_serialize_tds_settlement(wt))
                continue
            wr_id = _withdrawal_id_from_transaction(wt)
            wr = wr_by_id.get(wr_id) if wr_id else None
            if wt.tx_type == WalletTransaction.TxType.DEBIT:
                results.append(_serialize_withdrawal_debit(wt, wr))
            elif wt.tx_type == WalletTransaction.TxType.ADJUSTMENT:
                results.append(_serialize_withdrawal_refund(wt, wr))
    return results


def _apply_ledger_running_balance(user_id: int, results: list[dict[str, Any]]) -> None:
    balance = wallet_cash_balance(user_id)
    for row in results:
        bal_s = _fmt_money(balance)
        row["balance"] = bal_s
        row["running_balance"] = bal_s
        row["tds_deducted"] = row["tds"]
        row["net_credited"] = row["net"]
        balance -= row.pop("_balance_delta", ZERO)


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

    count_sql, union_sql, params = _ledger_union_sql_and_params(user, period=period, typ=typ)
    # Running balance is computed by walking backward from the current wallet
    # balance. For paginated pages, include the skipped newer rows in that walk
    # and slice them away after balances are assigned; otherwise page 2+ would
    # incorrectly restart from the current balance.
    prefix_limit = offset + page_size
    union_sql += " LIMIT %s"

    with connection.cursor() as cursor:
        cursor.execute(count_sql, params)
        total_count = cursor.fetchone()[0]
        cursor.execute(union_sql, params + [prefix_limit])
        keys = cursor.fetchall()

    walked_results = _hydrate_ledger_keys(user, keys, typ=typ)
    _apply_ledger_running_balance(user.pk, walked_results)
    results = walked_results[offset : offset + page_size]

    return {
        "rows": results,
        "total_count": total_count,
        "page": page,
        "page_size": page_size,
    }


def wallet_cash_balance(user_id: int) -> Decimal:
    w, _ = Wallet.objects.get_or_create(user_id=user_id)
    return w.cash_balance or ZERO


def wallet_total_earned(user_id: int) -> Decimal:
    """Lifetime earnings (sum of credited commissions + milestones, net of reversals).

    Used as the base for the earnings-ledger running balance so that withdrawals
    do not push the displayed running total negative.
    """
    w, _ = Wallet.objects.get_or_create(user_id=user_id)
    return w.total_earned or ZERO


# Hard upper bound to keep export queries bounded for very active members.
LEDGER_EXPORT_MAX_ROWS = 50_000


def build_ledger_export_rows(
    user: User,
    *,
    period: str,
    typ: str,
    limit: int = LEDGER_EXPORT_MAX_ROWS,
) -> dict[str, Any]:
    """
    Return all earnings-ledger rows for `user` matching the given filters,
    in the same shape as `build_ledger().rows`, plus running balance walked
    backwards from the current wallet cash balance.

    Used by the member earnings export endpoint (CSV / PDF). Capped at
    `LEDGER_EXPORT_MAX_ROWS` (newest first) so a runaway query cannot blow up
    the worker; callers should expose `truncated` to the UI when this trips.
    """
    cap = max(1, min(int(limit), LEDGER_EXPORT_MAX_ROWS))
    t = _parse_ledger_type(typ)

    count_sql, union_sql, params = _ledger_union_sql_and_params(user, period=period, typ=typ)
    union_sql += " LIMIT %s"

    with connection.cursor() as cursor:
        cursor.execute(count_sql, params)
        total_count = cursor.fetchone()[0]
        cursor.execute(union_sql, params + [cap])
        keys = cursor.fetchall()

    results = _hydrate_ledger_keys(user, keys, typ=t)
    _apply_ledger_running_balance(user.pk, results)

    return {
        "rows": results,
        "total_count": total_count,
        "returned_count": len(results),
        "truncated": total_count > len(results),
        "filters": {
            "period": _parse_period(period),
            "type": t,
        },
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
        slot_expiry_days = cfg.sponsor_slot_expiry_days if kind == "SLOT" else None
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
                "slot_expiry_days": slot_expiry_days,
                "is_current": wallet.current_band == band_num,
                "unlocked": wallet.current_band >= band_num,
                "progress_in_band_percent": round(progress, 2) if progress is not None else None,
            }
        )
    return out


def build_payouts_bundle(user: User, *, include_movements: bool) -> dict[str, Any]:
    def _mask_bank_account(acct: str | None) -> str | None:
        s = (acct or "").strip()
        if not s:
            return None
        last4 = s[-4:] if len(s) >= 4 else s
        return f"XXXX{last4}" if last4 else "XXXX"

    wallet = get_wallet_row(user)
    cfg = get_system_config()
    kyc_ok = user.kyc_status == User.KYCStatus.VERIFIED
    cooling_days = max(0, int(cfg.cooling_off_days))
    cool = cooling_snapshot(user=user, wallet=wallet, cooling_days=cooling_days)
    locked_balance = cool["locked_balance"]
    available_balance = cool["available_balance"]
    wd_qs = (
        WithdrawalRequest.objects.filter(user=user)
        .order_by("-id")[:4]
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
            "available_balance": str(available_balance),
            "locked_balance": str(locked_balance),
            "cooling_days": cool["cooling_days"],
            "total_earned": str(wallet.total_earned),
            # total_withdrawn is a nested breakdown: `total` (= legacy scalar
            # value), `already_paid_out` (status=PAID), and `held_for_review`
            # (status in PENDING/APPROVED/PROCESSING/FAILED). REJECTED requests
            # are restored to the wallet and excluded by construction.
            "total_withdrawn": build_withdrawn_block(wallet),
            "total_tds_deducted": str(wallet.total_tds_deducted),
            "tds_payable_194r": str(wallet.tds_payable),
            "fy_label": wallet.fy_label,
            "band_cash_withdrawn_fy": str(wallet.band_cash_withdrawn_fy),
            "current_band": wallet.current_band,
            "withdrawals_blocked": not kyc_ok,
            "kyc_status": user.kyc_status,
        },
        "bands": build_band_ladder(wallet, cfg),
        "withdrawals": withdrawals,
    }
    data["bank_details"] = {
        "account_number": _mask_bank_account(getattr(user, "bank_account_number", None)),
        "ifsc": ((getattr(user, "bank_ifsc", None) or "").strip().upper() or None),
        "bank_name": ((getattr(user, "bank_name", None) or "").strip() or None),
    }
    data["upi_id"] = ((getattr(user, "upi_id", None) or "").strip() or None)
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


def build_todays_earnings_for_dashboard(user: User) -> dict[str, str]:
    """
    Tile fields for the member dashboard: todays_earnings is the net sum credited today
    (direct + tree_passive + milestone_bonuses). Other keys are the same window for
    commissions/milestones; wallet_balance is current cash_balance.
    """
    since = _period_start("today")
    assert since is not None  # period "today" is always bounded
    end = since + timedelta(days=1)
    wallet = get_wallet_row(user)
    ct = CommissionLedger.CommissionType
    st = CommissionLedger.Status
    passive_types = (ct.UPLINE_L1, ct.UPLINE_L2, ct.UPLINE_L3)
    dec = DecimalField(max_digits=12, decimal_places=2)
    agg = CommissionLedger.objects.filter(
        recipient_id=user.pk,
        status=st.CREDITED,
        created_at__gte=since,
        created_at__lt=end,
    ).aggregate(
        direct_net=Sum(
            Case(
                When(commission_type=ct.DIRECT, then=F("net_amount")),
                default=ZERO,
                output_field=dec,
            )
        ),
        passive_net=Sum(
            Case(
                When(commission_type__in=passive_types, then=F("net_amount")),
                default=ZERO,
                output_field=dec,
            )
        ),
    )
    direct = agg["direct_net"] or ZERO
    passive = agg["passive_net"] or ZERO
    ms_sum = (
        MilestoneRecord.objects.filter(
            user_id=user.pk,
            status="CREDITED",
            created_at__gte=since,
            created_at__lt=end,
        ).aggregate(s=Sum("net_bonus"))["s"]
        or ZERO
    )
    total_today = direct + passive + ms_sum
    return {
        "todays_earnings": _fmt_money(total_today),
        "direct_commission": _fmt_money(direct),
        "milestone_bonuses": _fmt_money(ms_sum),
        "tree_passive": _fmt_money(passive),
        "wallet_balance": _fmt_money(wallet.cash_balance),
    }


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
        data["summary"] = build_ui_summary(user, wallet, cfg)
    if "ledger" in include:
        data["filters"] = {
            "period": _parse_period(period),
            "type": _parse_ledger_type(ledger_type),
            "periods": ["today", "7d", "30d", "fy", "all"],
            "types": [
                "all",
                "direct",
                "passive",
                "milestone",
                "withdrawal",
            ],
        }
        data["ledger"] = build_ledger(
            user,
            period=period,
            typ=ledger_type,
            page=page,
            page_size=page_size,
        )
    return data


def _parse_period(raw: str | None) -> str:
    p = (raw or "all").strip().lower()
    return p if p in {"today", "7d", "30d", "fy", "all"} else "all"
