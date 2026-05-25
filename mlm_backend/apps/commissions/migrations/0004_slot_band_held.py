from decimal import Decimal

from django.db import migrations, models


# Re-declare here so the migration is independent of any code drift in
# apps.wallet.bands (which is import-safe but conceptually a runtime module).
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
SLOT_BAND_NUMBERS = frozenset({2, 4, 6, 8})
ZERO = Decimal("0")


def _band_index_for_earnings(total: Decimal) -> int:
    if total < BAND_EDGES[0]:
        return 0
    for i in range(len(BAND_EDGES) - 1):
        low, high = BAND_EDGES[i], BAND_EDGES[i + 1]
        if low <= total < high:
            return i + 1
    return 9


def _backfill_slot_band_held(apps, schema_editor):
    """
    Repair already mis-credited wallets:

    - Walk every recipient's CommissionLedger + MilestoneRecord rows in
      chronological order, replaying the running `total_earned`.
    - For each row that landed while the recipient was in a slot band
      (2/4/6/8) at the moment of credit, set `slot_band_held=True`.
    - Decrement `wallet.cash_balance` (and `total_withdrawn`-aware clamp)
      by the sum of CREDITED held net amounts that were previously
      (incorrectly) added to cash. Reversed rows already net to zero in
      both the old and new world, so no extra wallet adjustment is needed
      for them.
    - Clean up the matching `WalletTransaction` CREDIT rows so the ledger
      and cooling-off snapshot stay consistent.
    """
    CommissionLedger = apps.get_model("commissions", "CommissionLedger")
    MilestoneRecord = apps.get_model("commissions", "MilestoneRecord")
    Wallet = apps.get_model("wallet", "Wallet")
    WalletTransaction = apps.get_model("wallet", "WalletTransaction")
    Order = apps.get_model("payments", "Order")

    # Gather every recipient/user that has any commission or milestone row.
    recipient_ids = set(
        CommissionLedger.objects.values_list("recipient_id", flat=True).distinct()
    )
    recipient_ids.update(
        MilestoneRecord.objects.values_list("user_id", flat=True).distinct()
    )

    order_number_by_id = dict(
        Order.objects.filter(
            id__in=set(CommissionLedger.objects.values_list("order_id", flat=True))
        ).values_list("id", "order_number")
    )

    for uid in recipient_ids:
        commission_rows = list(
            CommissionLedger.objects.filter(recipient_id=uid)
            .order_by("created_at", "id")
        )
        milestone_rows = list(
            MilestoneRecord.objects.filter(user_id=uid).order_by("created_at", "id")
        )

        # Interleave by created_at while preserving stable ordering.
        merged = [
            ("c", r.created_at, r) for r in commission_rows
        ] + [
            ("m", r.created_at, r) for r in milestone_rows
        ]
        merged.sort(key=lambda x: (x[1], 0 if x[0] == "c" else 1, x[2].id))

        running_total = ZERO
        held_to_correct: list[tuple[str, object, Decimal]] = []

        for kind, _ca, row in merged:
            net = row.net_amount if kind == "c" else row.net_bonus
            net = net or ZERO
            status_ok_for_replay = (
                (kind == "c" and row.status in ("CREDITED", "REVERSED"))
                or (kind == "m" and row.status == "CREDITED")
            )
            if not status_ok_for_replay:
                # PENDING / HELD never contributed to running_total.
                continue

            band_before = _band_index_for_earnings(running_total)
            held = band_before in SLOT_BAND_NUMBERS
            if held and not row.slot_band_held:
                row.slot_band_held = True
                row.save(update_fields=["slot_band_held"])
            running_total += net
            if held and row.status == "CREDITED":
                held_to_correct.append((kind, row, net))

        if not held_to_correct:
            continue

        wallet = Wallet.objects.filter(user_id=uid).first()
        if wallet is None:
            continue
        correction = sum((net for _k, _r, net in held_to_correct), ZERO)
        if correction <= ZERO:
            continue
        # Clamp so cash_balance never goes negative — withdrawals may have
        # already drained part of the mis-credited amount.
        clamped = min(correction, wallet.cash_balance or ZERO)
        if clamped > ZERO:
            wallet.cash_balance = (wallet.cash_balance or ZERO) - clamped
            wallet.save(update_fields=["cash_balance"])

        # Delete the matching WalletTransaction CREDIT rows so the ledger
        # walk and cooling-off snapshot don't double-count.
        for kind, row, _net in held_to_correct:
            if kind == "c":
                order_no = order_number_by_id.get(row.order_id)
                if not order_no:
                    continue
                WalletTransaction.objects.filter(
                    user_id=uid,
                    tx_type="CREDIT",
                    reference=f"COMM-{order_no}",
                    amount=row.net_amount,
                ).delete()
            else:
                WalletTransaction.objects.filter(
                    user_id=uid,
                    tx_type="CREDIT",
                    reference=f"MILESTONE-{row.milestone_referrals}",
                    amount=row.net_bonus,
                ).delete()


def _noop_reverse(apps, schema_editor):
    # The forward backfill cannot be safely reversed: we have no record of
    # which WalletTransaction CREDIT rows we deleted. The schema-level
    # field removal is handled by Django automatically when the schema is
    # reversed.
    return None


class Migration(migrations.Migration):

    dependencies = [
        ("commissions", "0003_initial"),
        ("wallet", "0002_withdrawal_request_lifecycle_fields"),
        ("payments", "0012_refundrequest_order_line"),
    ]

    operations = [
        migrations.AddField(
            model_name="commissionledger",
            name="slot_band_held",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "True when this credit landed while the recipient was inside a "
                    "slot band (2/4/6/8). The amount still counts toward total_earned "
                    "and the cap, but is NOT added to cash_balance / "
                    "available_to_withdraw."
                ),
            ),
        ),
        migrations.AddField(
            model_name="milestonerecord",
            name="slot_band_held",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "True when this milestone credit landed while the recipient was "
                    "inside a slot band (2/4/6/8); counts toward total_earned but "
                    "not cash_balance."
                ),
            ),
        ),
        migrations.RunPython(_backfill_slot_band_held, _noop_reverse),
    ]
