from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from apps.tds.models import TdsLedger
from apps.users.models import User


TDS_THRESHOLD = Decimal("20000.00")
RATE_PAN = Decimal("0.02")
RATE_NO_PAN = Decimal("0.20")
ZERO = Decimal("0.00")
TWO_PLACES = Decimal("0.01")


def get_current_financial_year(*, at: datetime | None = None) -> str:
    """
    India FY label: Apr 1 - Mar 31, formatted as 'YYYY-YY' (e.g. '2025-26').
    """
    d = timezone.localdate(at or timezone.now())
    year = d.year
    if d.month >= 4:
        return f"{year}-{str(year + 1)[-2:]}"
    return f"{year - 1}-{str(year)[-2:]}"


def get_194h_rate_for_user(user: User) -> Decimal:
    """
    Sec 194H: 2% when PAN present, else 20%.
    Note: platform policy may ensure PAN exists for KYC VERIFIED, but we keep fallback.
    """
    pan = (getattr(user, "pan_number", None) or "").strip()
    return RATE_PAN if pan else RATE_NO_PAN


def _q2(v: Decimal) -> Decimal:
    return (v or ZERO).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class TdsResult:
    gross_amount: Decimal
    tds_amount: Decimal
    net_amount: Decimal
    tds_rate_percent: Decimal
    tds_applicable: bool
    financial_year: str


@transaction.atomic
def calculate_and_apply_194h_tds(*, user: User, gross_amount: Decimal) -> TdsResult:
    """
    Implements the cumulative-FY 'catch-up' logic from tds-implementation-logic.md.
    Updates (or creates) the per-user-per-FY TdsLedger row under select_for_update.
    """
    gross_amount = _q2(gross_amount)
    fy = get_current_financial_year()
    rate = get_194h_rate_for_user(user)

    ledger, _ = TdsLedger.objects.select_for_update().get_or_create(
        user=user,
        financial_year=fy,
        defaults={
            "total_earned": ZERO,
            "total_tds": ZERO,
            "tds_triggered": False,
            "tds_triggered_at": None,
        },
    )

    prev_total = _q2(ledger.total_earned)
    new_total = _q2(prev_total + gross_amount)

    tds_amount = ZERO
    tds_applicable = False

    if ledger.tds_triggered:
        required_total = _q2(new_total * rate)
        already = _q2(ledger.total_tds)
        tds_amount = _q2(required_total - already)
        if tds_amount < ZERO:
            tds_amount = ZERO
        tds_applicable = True
    elif new_total > TDS_THRESHOLD:
        required_total = _q2(new_total * rate)
        already = _q2(ledger.total_tds)
        tds_amount = _q2(required_total - already)
        if tds_amount < ZERO:
            tds_amount = ZERO
        tds_applicable = True
        ledger.tds_triggered = True
        ledger.tds_triggered_at = timezone.now()

    net_amount = _q2(gross_amount - tds_amount)
    if net_amount < ZERO:
        # Defensive: never credit negative net; cap TDS at gross.
        tds_amount = gross_amount
        net_amount = ZERO

    ledger.total_earned = _q2(ledger.total_earned + gross_amount)
    ledger.total_tds = _q2(ledger.total_tds + tds_amount)
    ledger.save(update_fields=["total_earned", "total_tds", "tds_triggered", "tds_triggered_at", "updated_at"])

    return TdsResult(
        gross_amount=gross_amount,
        tds_amount=tds_amount,
        net_amount=net_amount,
        tds_rate_percent=_q2(rate * Decimal("100")),
        tds_applicable=tds_applicable and tds_amount > ZERO,
        financial_year=fy,
    )

