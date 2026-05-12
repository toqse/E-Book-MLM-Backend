from decimal import Decimal

# (referral_threshold, unused_pct, bonus_gross) — pct is reserved; engine uses threshold + bonus only.
DEFAULT_MILESTONES: list[tuple[int, Decimal, Decimal]] = [
    (10, Decimal("0.15"), Decimal("300")),
    (25, Decimal("0.12"), Decimal("600")),
    (50, Decimal("0.10"), Decimal("1000")),
    (75, Decimal("0.09"), Decimal("1350")),
    (100, Decimal("0.08"), Decimal("1600")),
]


def get_milestones(cfg=None) -> list[tuple[int, Decimal, Decimal]]:
    """
    Return milestone tiers, applying SystemConfig overrides when present.

    Overrides are stored on SystemConfig.milestone_bonus_overrides as a mapping:
      { "<threshold>": "<bonus>" } or { <threshold>: <bonus> }
    """
    out: list[tuple[int, Decimal, Decimal]] = []
    raw_overrides = getattr(cfg, "milestone_bonus_overrides", None) if cfg is not None else None
    overrides = raw_overrides if isinstance(raw_overrides, dict) else {}

    for th, pct, bonus in DEFAULT_MILESTONES:
        key_candidates = (th, str(th))
        if any(k in overrides for k in key_candidates):
            raw_bonus = overrides.get(th, overrides.get(str(th)))
            try:
                bonus = Decimal(str(raw_bonus))
            except Exception:
                bonus = bonus
        out.append((int(th), pct, bonus))
    return out


# Backward-compatible alias for older imports.
MILESTONES = DEFAULT_MILESTONES
