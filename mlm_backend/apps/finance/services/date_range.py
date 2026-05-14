"""Shared date windows for Admin Finance (local timezone, inclusive dates)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_date as django_parse_date


@dataclass(frozen=True)
class FinanceDateRange:
    """Inclusive calendar-date range plus the prior window of equal length (for trends)."""

    date_from: date
    date_to: date
    previous_date_from: date
    previous_date_to: date
    preset: str | None

    @property
    def inclusive_days(self) -> int:
        return (self.date_to - self.date_from).days + 1


def _local_today() -> date:
    return timezone.localdate()


def _indian_fy_bounds_for(containing: date) -> tuple[date, date]:
    """FY YYYY-(YY+1): 1 Apr YYYY → 31 Mar YYYY+1."""
    y = containing.year
    if containing.month >= 4:
        start_y = y
    else:
        start_y = y - 1
    return date(start_y, 4, 1), date(start_y + 1, 3, 31)


def _parse_fy_label(raw: str) -> tuple[date, date] | None:
    """
    Accept '2025-26' or '2025-2026' → 2025-04-01 .. 2026-03-31.
    """
    s = (raw or "").strip().replace(" ", "")
    if not s:
        return None
    if "-" in s:
        parts = s.split("-", 1)
        if len(parts) != 2:
            return None
        a, b = parts[0].strip(), parts[1].strip()
        try:
            y1 = int(a[:4])
        except ValueError:
            return None
        if len(b) == 2 and b.isdigit():
            y2_century = (y1 // 100) * 100
            y2 = y2_century + int(b)
            if y2 < y1:
                y2 += 100
        else:
            try:
                y2 = int(b[:4])
            except ValueError:
                return None
        if y2 != y1 + 1:
            return None
        return date(y1, 4, 1), date(y2, 3, 31)
    return None


def _previous_window(d0: date, d1: date) -> tuple[date, date]:
    days = (d1 - d0).days + 1
    prev_to = d0 - timedelta(days=1)
    prev_from = prev_to - timedelta(days=days - 1)
    return prev_from, prev_to


def parse_finance_range(
    query_params: dict[str, Any] | None = None,
    *,
    body: dict[str, Any] | None = None,
) -> FinanceDateRange:
    """
    Resolve `from` / `to` (YYYY-MM-DD) or `preset` (today|7d|30d|fy).

    Priority: explicit from+to > fy= label > preset.
    If only one of from/to is sent, the missing side is inferred where reasonable.
    Default when nothing valid: preset 30d ending today.
    """
    qp = query_params or {}
    bd = body or {}

    def _g(key: str) -> str:
        v = qp.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
        v2 = bd.get(key)
        if v2 is not None and str(v2).strip():
            return str(v2).strip()
        return ""

    raw_from = _g("from")
    raw_to = _g("to")
    preset = (_g("preset") or "").strip().lower() or None
    fy_label = _g("fy")

    d_from = django_parse_date(raw_from) if raw_from else None
    d_to = django_parse_date(raw_to) if raw_to else None

    used_preset: str | None = None
    today = _local_today()

    if d_from and d_to:
        if d_from > d_to:
            d_from, d_to = d_to, d_from
    elif fy_label and (bounds := _parse_fy_label(fy_label)):
        d_from, d_to = bounds
        used_preset = "fy_label"
    elif preset in ("today", "7d", "30d", "fy"):
        used_preset = preset
        if preset == "today":
            d_from = d_to = today
        elif preset == "7d":
            d_to = today
            d_from = today - timedelta(days=6)
        elif preset == "30d":
            d_to = today
            d_from = today - timedelta(days=29)
        else:  # fy
            d_from, d_to = _indian_fy_bounds_for(today)
    elif d_from and not d_to:
        d_to = min(d_from + timedelta(days=29), today)
        if d_to < d_from:
            d_to = d_from
    elif d_to and not d_from:
        d_from = d_to - timedelta(days=29)
    else:
        d_to = today
        d_from = today - timedelta(days=29)
        used_preset = "30d"

    assert d_from is not None and d_to is not None
    if d_from > d_to:
        d_from, d_to = d_to, d_from

    prev_from, prev_to = _previous_window(d_from, d_to)
    return FinanceDateRange(
        date_from=d_from,
        date_to=d_to,
        previous_date_from=prev_from,
        previous_date_to=prev_to,
        preset=used_preset or preset,
    )
