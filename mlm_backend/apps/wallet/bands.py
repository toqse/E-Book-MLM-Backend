from decimal import Decimal

from apps.admin_panel.utils import get_system_config


# Cumulative earnings thresholds (₹) — aligned with PDF band table
BAND_EDGES = [
    Decimal("200"),
    Decimal("4000"),
    Decimal("5000"),
    Decimal("9000"),
    Decimal("10000"),
    Decimal("14000"),
    Decimal("15000"),
    Decimal("19000"),
    Decimal("20000"),
    Decimal("22200"),
]

# Bands where commissions and milestone bonuses fund sponsor-slot issuance
# instead of cash. Credits earned in these bands bump total_earned but NOT
# cash_balance; the recipient cannot withdraw them.
SLOT_BAND_NUMBERS = frozenset({2, 4, 6, 8})


def is_slot_band(band_number: int | None) -> bool:
    return int(band_number or 0) in SLOT_BAND_NUMBERS


def _band_index_for_earnings(total: Decimal) -> int:
    if total < BAND_EDGES[0]:
        return 0
    for i in range(len(BAND_EDGES) - 1):
        low, high = BAND_EDGES[i], BAND_EDGES[i + 1]
        if low <= total < high:
            return i + 1
    return 9


def _next_band_edge(total: Decimal) -> Decimal | None:
    """Upper cumulative-earnings threshold for the band `total` is currently in."""
    band = _band_index_for_earnings(total)
    if band == 0:
        return BAND_EDGES[0]
    if band >= len(BAND_EDGES):
        return None
    return BAND_EDGES[band]


def iter_band_split_pieces(
    *,
    total_earned: Decimal,
    gross: Decimal,
    cap: Decimal | None = None,
):
    """
    Yield (piece_amount, slot_band_held) splitting `gross` at each band edge.

    When `cap` is set, stops once ``total_earned + credited`` would exceed it.
    Caller must ensure ``gross <= cap - total_earned`` when cap applies.
    """
    remaining = gross
    cursor = total_earned
    while remaining > 0:
        if cap is not None:
            cap_room = cap - cursor
            if cap_room <= 0:
                break
        else:
            cap_room = remaining

        edge = _next_band_edge(cursor)
        if edge is None:
            piece = min(remaining, cap_room)
        else:
            band_room = edge - cursor
            piece = min(remaining, band_room, cap_room)

        if piece <= 0:
            break

        band_before = _band_index_for_earnings(cursor)
        yield piece, band_before in SLOT_BAND_NUMBERS
        cursor += piece
        remaining -= piece


def slot_gross_if_split_at(*, total_before: Decimal, amount: Decimal) -> Decimal:
    """Slot-tagged gross if `amount` were credited with band-split routing."""
    slot = Decimal("0")
    for piece, is_slot in iter_band_split_pieces(
        total_earned=total_before, gross=amount, cap=None
    ):
        if is_slot:
            slot += piece
    return slot


def on_total_earned_updated(wallet):
    idx = _band_index_for_earnings(wallet.total_earned)
    prev = wallet.current_band
    cfg = get_system_config()
    # Always attempt progressive sponsor-slot unlocks when earnings change.
    try:
        from apps.sponsor_slots.services import SponsorSlotService

        SponsorSlotService.unlock_due_codes(
            user=wallet.user, total_earned=wallet.total_earned
        )
    except Exception:
        # Unlock should never break commission crediting; failures are non-critical.
        pass
    if idx <= prev:
        return
    wallet.current_band = idx
    wallet.save(update_fields=["current_band"])
    if idx in SLOT_BAND_NUMBERS:
        from apps.sponsor_slots.models import SponsorSlotBatch
        from apps.sponsor_slots.services import SponsorSlotService

        if not SponsorSlotBatch.objects.filter(
            issued_to=wallet.user, band_number=idx
        ).exists():
            SponsorSlotService.issue_batch(
                wallet.user,
                band_number=idx,
                cfg=cfg,
                current_total_earned=wallet.total_earned,
            )


def describe_bands_status(wallet) -> list[dict]:
    out = []
    for i in range(1, 10):
        out.append({"band": i, "unlocked": wallet.current_band >= i})
    return out
