"""
One-shot reconciliation: move legacy slot-band overshoot gross into cash_balance.

Historical CommissionLedger / MilestoneRecord rows are not mutated. Each affected
wallet receives a single WalletTransaction(ADJUSTMENT) when overshoot > 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.commissions.models import CommissionLedger, MilestoneRecord
from apps.users.models import User
from apps.wallet.bands import slot_gross_if_split_at
from apps.wallet.models import Wallet, WalletTransaction

ZERO = Decimal("0")
RECONCILE_REF_PREFIX = "RECONCILE-SLOT-OVERSHOOT"


@dataclass
class ReconcileResult:
    wallet_id: int
    user_id: int
    member_id: str
    slot_total_current: Decimal
    slot_total_correct: Decimal
    overshoot: Decimal
    action: str  # skip | dry_run | applied | already_done


def _slot_total_current(*, user_id: int) -> Decimal:
    comm = (
        CommissionLedger.objects.filter(
            recipient_id=user_id,
            status=CommissionLedger.Status.CREDITED,
            slot_band_held=True,
        ).aggregate(s=Sum("amount"))["s"]
        or ZERO
    )
    ms = (
        MilestoneRecord.objects.filter(
            user_id=user_id,
            status="CREDITED",
            slot_band_held=True,
        ).aggregate(s=Sum("bonus_amount"))["s"]
        or ZERO
    )
    return comm + ms


def _compute_overshoot(*, user_id: int) -> tuple[Decimal, Decimal, Decimal]:
    """
    Walk credited rows chronologically.

    Returns (slot_total_current, slot_total_correct, overshoot) where
    overshoot is cash that was wrongly left non-withdrawable on slot-tagged rows.
    """
    events: list[tuple[Any, Decimal, bool]] = []
    for row in CommissionLedger.objects.filter(
        recipient_id=user_id,
        status=CommissionLedger.Status.CREDITED,
    ).only("created_at", "amount", "slot_band_held"):
        events.append(
            (row.created_at, row.amount or ZERO, bool(row.slot_band_held))
        )
    for row in MilestoneRecord.objects.filter(
        user_id=user_id,
        status="CREDITED",
    ).only("created_at", "bonus_amount", "slot_band_held"):
        events.append(
            (row.created_at, row.bonus_amount or ZERO, bool(row.slot_band_held))
        )

    events.sort(key=lambda x: x[0])
    cursor = ZERO
    slot_current = ZERO
    slot_correct = ZERO
    overshoot = ZERO
    for _, amount, is_slot in events:
        if amount <= ZERO:
            continue
        should_be_slot = slot_gross_if_split_at(total_before=cursor, amount=amount)
        if is_slot:
            slot_current += amount
            slot_correct += should_be_slot
            piece_overshoot = amount - should_be_slot
            if piece_overshoot > ZERO:
                overshoot += piece_overshoot
        else:
            slot_correct += should_be_slot
        cursor += amount
    return slot_current, slot_correct, overshoot


def _has_tds_blockers(*, wallet: Wallet, user_id: int) -> bool:
    if (wallet.tds_payable or ZERO) > ZERO:
        return True
    if (wallet.total_tds_deducted or ZERO) > ZERO:
        return True
    if CommissionLedger.objects.filter(
        recipient_id=user_id,
        status=CommissionLedger.Status.CREDITED,
        slot_band_held=True,
        tds_deducted__gt=ZERO,
    ).exists():
        return True
    if MilestoneRecord.objects.filter(
        user_id=user_id,
        status="CREDITED",
        slot_band_held=True,
        tds_deducted__gt=ZERO,
    ).exists():
        return True
    return False


def reconcile_wallet(
    wallet: Wallet,
    *,
    apply: bool,
    dry_run: bool,
) -> ReconcileResult:
    user = wallet.user
    ref = f"{RECONCILE_REF_PREFIX}:{wallet.pk}"
    if WalletTransaction.objects.filter(
        user_id=user.pk, reference=ref
    ).exists():
        return ReconcileResult(
            wallet_id=wallet.pk,
            user_id=user.pk,
            member_id=user.member_id or "",
            slot_total_current=ZERO,
            slot_total_correct=ZERO,
            overshoot=ZERO,
            action="already_done",
        )

    slot_current, slot_correct, overshoot = _compute_overshoot(user_id=user.pk)
    if overshoot <= ZERO:
        return ReconcileResult(
            wallet_id=wallet.pk,
            user_id=user.pk,
            member_id=user.member_id or "",
            slot_total_current=slot_current,
            slot_total_correct=slot_correct,
            overshoot=ZERO,
            action="skip",
        )

    if _has_tds_blockers(wallet=wallet, user_id=user.pk):
        return ReconcileResult(
            wallet_id=wallet.pk,
            user_id=user.pk,
            member_id=user.member_id or "",
            slot_total_current=slot_current,
            slot_total_correct=slot_correct,
            overshoot=overshoot,
            action="skip_tds",
        )

    if dry_run or not apply:
        return ReconcileResult(
            wallet_id=wallet.pk,
            user_id=user.pk,
            member_id=user.member_id or "",
            slot_total_current=slot_current,
            slot_total_correct=slot_correct,
            overshoot=overshoot,
            action="dry_run",
        )

    with transaction.atomic():
        wallet = Wallet.objects.select_for_update().get(pk=wallet.pk)
        if WalletTransaction.objects.filter(
            user_id=user.pk, reference=ref
        ).exists():
            return ReconcileResult(
                wallet_id=wallet.pk,
                user_id=user.pk,
                member_id=user.member_id or "",
                slot_total_current=slot_current,
                slot_total_correct=slot_correct,
                overshoot=overshoot,
                action="already_done",
            )
        new_balance = (wallet.cash_balance or ZERO) + overshoot
        WalletTransaction.objects.create(
            user=user,
            tx_type=WalletTransaction.TxType.ADJUSTMENT,
            amount=overshoot,
            balance_after=new_balance,
            reference=ref,
            meta={
                "kind": "slot_overshoot_reconcile",
                "slot_total_current": str(slot_current),
                "slot_total_correct": str(slot_correct),
                "overshoot": str(overshoot),
                "applied_at": timezone.now().isoformat(),
            },
        )
        wallet.cash_balance = new_balance
        wallet.save(update_fields=["cash_balance", "updated_at"])

    return ReconcileResult(
        wallet_id=wallet.pk,
        user_id=user.pk,
        member_id=user.member_id or "",
        slot_total_current=slot_current,
        slot_total_correct=slot_correct,
        overshoot=overshoot,
        action="applied",
    )


class Command(BaseCommand):
    help = (
        "Reconcile slot-band overshoot: credit cash_balance for gross that was "
        "historically tagged slot but should have been cash under band-split routing."
    )

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument(
            "--dry-run",
            action="store_true",
            help="Report overshoot per wallet without writing adjustments.",
        )
        group.add_argument(
            "--apply",
            action="store_true",
            help="Write ADJUSTMENT rows and bump cash_balance.",
        )
        parser.add_argument(
            "--user-id",
            type=int,
            default=None,
            help="Limit to a single user id.",
        )

    def handle(self, *args, **options):
        apply = bool(options["apply"])
        dry_run = bool(options["dry_run"])
        user_id = options.get("user_id")

        qs = Wallet.objects.select_related("user").order_by("pk")
        if user_id is not None:
            qs = qs.filter(user_id=user_id)
            if not qs.exists():
                raise CommandError(f"No wallet for user_id={user_id}")

        counts: dict[str, int] = {}
        total_overshoot = ZERO

        for wallet in qs.iterator():
            result = reconcile_wallet(wallet, apply=apply, dry_run=dry_run)
            counts[result.action] = counts.get(result.action, 0) + 1
            if result.action in ("dry_run", "applied"):
                total_overshoot += result.overshoot
                self.stdout.write(
                    f"user={result.member_id} wallet={result.wallet_id} "
                    f"slot_current={result.slot_total_current} "
                    f"slot_correct={result.slot_total_correct} "
                    f"overshoot={result.overshoot} -> {result.action}"
                )
            elif result.action == "skip_tds":
                self.stdout.write(
                    self.style.WARNING(
                        f"SKIP (TDS) user={result.member_id} wallet={result.wallet_id} "
                        f"overshoot={result.overshoot}"
                    )
                )

        mode = "APPLY" if apply else "DRY-RUN"
        self.stdout.write(
            self.style.SUCCESS(
                f"\n{mode} complete: {counts} total_overshoot={total_overshoot}"
            )
        )
