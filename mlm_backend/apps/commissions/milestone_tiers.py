from decimal import Decimal

# (referral_threshold, unused_pct, bonus_gross) — pct is reserved; engine uses threshold + bonus only.
MILESTONES: list[tuple[int, Decimal, Decimal]] = [
    (10, Decimal("0.15"), Decimal("300")),
    (25, Decimal("0.12"), Decimal("600")),
    (50, Decimal("0.10"), Decimal("1000")),
    (75, Decimal("0.09"), Decimal("1350")),
    (100, Decimal("0.08"), Decimal("1600")),
]
