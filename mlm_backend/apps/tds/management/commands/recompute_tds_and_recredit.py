"""
Recompute FY TdsLedger from credited commission/milestone rows for both
Sec 194H (cash bands) and Sec 194R (slot bands), and re-credit members
who were over-withheld due to the false 20k trigger.

Usage:
  python manage.py recompute_tds_and_recredit --fy 2026-27 --dry-run
  python manage.py recompute_tds_and_recredit --fy 2026-27 --apply
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.audit.services import write_audit
from apps.commissions.models import CommissionLedger, MilestoneRecord
from apps.tds.models import TdsLedger
from apps.tds.services import (
    TDS_THRESHOLD,
    TDS_THRESHOLD_194R,
    get_194h_rate_for_user,
    get_194r_rate_for_user,
    get_current_financial_year,
)
from apps.users.models import User
from apps.wallet.bands import _band_index_for_earnings, on_total_earned_updated
from apps.wallet.models import Wallet, WalletTransaction

ZERO = Decimal("0.00")
TWO = Decimal("0.01")


def _q2(v: Decimal) -> Decimal:
    return (v or ZERO).quantize(TWO)


def _fy_start_dt(fy_label: str) -> datetime:
    """FY label '2026-27' -> aware datetime Apr 1 2026 00:00."""
    start_year = int(str(fy_label).split("-")[0])
    dt = datetime(start_year, 4, 1, 0, 0, 0)
    if settings.USE_TZ:
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


@dataclass
class _Allocation:
    correct_gross: Decimal
    correct_tds: Decimal
    triggered: bool
    by_row: dict[tuple[str, int], Decimal]


def _allocate_correct_tds(
    rows: list[tuple[str, object]],
    *,
    rate: Decimal,
    threshold: Decimal,
) -> _Allocation:
    """
    Replay FY TDS calc across chronologically ordered credited rows of a
    single TDS section. Yields per-row TDS so we can normalize the stored
    `tds_deducted` and `net_amount` fields.
    """
    total_earned = ZERO
    total_tds = ZERO
    triggered = False
    by_key: dict[tuple[str, int], Decimal] = {}

    for kind, row in rows:
        gross = _q2(row.amount if kind == "commission" else row.bonus_amount)
        new_total = _q2(total_earned + gross)
        tds_amount = ZERO
        if triggered or new_total > threshold:
            required_total = _q2(new_total * rate)
            tds_amount = max(ZERO, _q2(required_total - total_tds))
            if tds_amount > gross:
                tds_amount = gross
            triggered = True
        total_earned = new_total
        total_tds = _q2(total_tds + tds_amount)
        by_key[(kind, row.pk)] = tds_amount

    return _Allocation(total_earned, total_tds, triggered, by_key)


class Command(BaseCommand):
    help = "Recompute TDS FY ledger (194H + 194R) and re-credit over-withheld amounts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fy",
            default=None,
            help="Financial year label (default: current FY)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print changes without writing",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply corrections",
        )
        parser.add_argument(
            "--member-id",
            default=None,
            help="Limit to one member_id (e.g. JST000011)",
        )

    def handle(self, *args, **options):
        if not options["dry_run"] and not options["apply"]:
            self.stderr.write("Specify --dry-run or --apply")
            return
        fy = options["fy"] or get_current_financial_year()
        fy_start = _fy_start_dt(fy)
        member_filter = options.get("member_id")

        users = User.objects.all().order_by("id")
        if member_filter:
            users = users.filter(member_id=member_filter)

        self.stdout.write(
            f"FY={fy}  dry_run={options['dry_run']}  apply={options['apply']}"
        )
        self.stdout.write(
            "member_id | section | before_earned | after_earned | "
            "before_tds | after_tds | recredit/payable | triggered_after"
        )

        for user in users.iterator():
            self._process_user_section(
                user,
                fy=fy,
                fy_start=fy_start,
                section=TdsLedger.SECTION_194H,
                dry_run=options["dry_run"],
                apply=options["apply"],
            )
            self._process_user_section(
                user,
                fy=fy,
                fy_start=fy_start,
                section=TdsLedger.SECTION_194R,
                dry_run=options["dry_run"],
                apply=options["apply"],
            )
            self._sync_wallet_total_earned(
                user,
                fy_start=fy_start,
                dry_run=options["dry_run"],
                apply=options["apply"],
            )

    def _process_user_section(
        self,
        user: User,
        *,
        fy: str,
        fy_start: datetime,
        section: str,
        dry_run: bool,
        apply: bool,
    ) -> None:
        is_194r = section == TdsLedger.SECTION_194R
        rate = get_194r_rate_for_user(user) if is_194r else get_194h_rate_for_user(user)
        threshold = TDS_THRESHOLD_194R if is_194r else TDS_THRESHOLD
        slot_filter = is_194r  # 194R rows are slot-band; 194H rows are cash-band

        comm_qs = CommissionLedger.objects.filter(
            recipient_id=user.pk,
            status=CommissionLedger.Status.CREDITED,
            created_at__gte=fy_start,
            slot_band_held=slot_filter,
        )
        ms_qs = MilestoneRecord.objects.filter(
            user_id=user.pk,
            status="CREDITED",
            created_at__gte=fy_start,
            slot_band_held=slot_filter,
        )

        rows: list[tuple[str, object]] = [
            ("commission", row) for row in comm_qs.order_by("created_at", "id")
        ] + [
            ("milestone", row) for row in ms_qs.order_by("created_at", "id")
        ]
        rows.sort(key=lambda item: (item[1].created_at, item[1].pk))

        alloc = _allocate_correct_tds(rows, rate=rate, threshold=threshold)
        actually_withheld = _q2(
            (comm_qs.aggregate(s=Sum("tds_deducted"))["s"] or ZERO)
            + (ms_qs.aggregate(s=Sum("tds_deducted"))["s"] or ZERO)
        )

        ledger = TdsLedger.objects.filter(
            user_id=user.pk, financial_year=fy, section=section
        ).first()
        before_ledger_earned = _q2(ledger.total_earned if ledger else ZERO)
        before_ledger_tds = _q2(ledger.total_tds if ledger else ZERO)

        rows_need_normalization = any(
            _q2(row.tds_deducted) != alloc.by_row[(kind, row.pk)]
            or self._row_net_needs_update(kind, row, alloc.by_row[(kind, row.pk)], is_194r)
            for kind, row in rows
        )

        if is_194r:
            # Sec 194R obligations are tracked in wallet.tds_payable; settled
            # later by `settle_tds_payable`. We compute new_payable from the
            # correct cumulative liability minus what's already been settled
            # via WalletTransaction(TxType.TDS, ref TDS-194R-SETTLE%).
            settled_so_far = _q2(
                WalletTransaction.objects.filter(
                    user_id=user.pk,
                    tx_type=WalletTransaction.TxType.TDS,
                    reference__startswith="TDS-194R-SETTLE",
                    created_at__gte=fy_start,
                ).aggregate(s=Sum("amount"))["s"]
                or ZERO
            )
            new_payable = _q2(max(ZERO, alloc.correct_tds - settled_so_far))
            recredit_or_payable = new_payable
        else:
            recredit_or_payable = _q2(max(ZERO, actually_withheld - alloc.correct_tds))

        no_change = (
            before_ledger_earned == alloc.correct_gross
            and before_ledger_tds == alloc.correct_tds
            and not rows_need_normalization
        )
        if no_change:
            if is_194r:
                # Still verify wallet.tds_payable matches new_payable; if equal,
                # short-circuit; otherwise fall through to apply the fix.
                wallet = Wallet.objects.filter(user_id=user.pk).first()
                if wallet and _q2(wallet.tds_payable) == recredit_or_payable:
                    return
            else:
                if recredit_or_payable <= ZERO:
                    return

        self.stdout.write(
            f"{user.member_id} | {section} | {before_ledger_earned} | "
            f"{alloc.correct_gross} | {before_ledger_tds} | "
            f"{alloc.correct_tds} | {recredit_or_payable} | {alloc.triggered}"
        )

        if dry_run and not apply:
            return

        with transaction.atomic():
            wallet, _ = Wallet.objects.select_for_update().get_or_create(user=user)
            ledger, _ = TdsLedger.objects.select_for_update().get_or_create(
                user=user,
                financial_year=fy,
                section=section,
                defaults={
                    "total_earned": ZERO,
                    "total_tds": ZERO,
                    "tds_triggered": False,
                    "tds_triggered_at": None,
                },
            )
            ledger.total_earned = alloc.correct_gross
            ledger.total_tds = alloc.correct_tds
            ledger.tds_triggered = alloc.correct_tds > ZERO
            if not ledger.tds_triggered:
                ledger.tds_triggered_at = None
            ledger.save(
                update_fields=[
                    "total_earned",
                    "total_tds",
                    "tds_triggered",
                    "tds_triggered_at",
                    "updated_at",
                ]
            )

            if is_194r:
                wallet.tds_payable = recredit_or_payable
            else:
                if recredit_or_payable > ZERO:
                    correction_ref = f"TDS-CORRECTION-{fy}"
                    already_corrected = WalletTransaction.objects.filter(
                        user_id=user.pk,
                        reference=correction_ref,
                    ).exists()
                    if not already_corrected:
                        wallet.cash_balance = _q2(
                            (wallet.cash_balance or ZERO) + recredit_or_payable
                        )
                        wallet.total_tds_deducted = _q2(
                            max(
                                ZERO,
                                (wallet.total_tds_deducted or ZERO)
                                - recredit_or_payable,
                            )
                        )
                        WalletTransaction.objects.create(
                            user=user,
                            tx_type=WalletTransaction.TxType.ADJUSTMENT,
                            amount=recredit_or_payable,
                            balance_after=wallet.cash_balance,
                            reference=correction_ref,
                            meta={
                                "kind": "tds_overwithholding_reversal",
                                "section": section,
                                "fy": fy,
                                "reason": "recompute",
                                "before_ledger_earned": str(before_ledger_earned),
                                "after_ledger_earned": str(alloc.correct_gross),
                                "before_ledger_tds": str(before_ledger_tds),
                                "after_ledger_tds": str(alloc.correct_tds),
                            },
                        )

            if rows_need_normalization:
                for kind, row in rows:
                    row_tds = alloc.by_row[(kind, row.pk)]
                    if kind == "commission":
                        row = CommissionLedger.objects.select_for_update().get(pk=row.pk)
                        row.tds_deducted = row_tds
                        # 194R: TDS sits in tds_payable; cash net stays equal to gross.
                        # 194H: TDS is withheld from cash; net = gross - tds.
                        gross = row.amount or ZERO
                        row.net_amount = gross if is_194r else _q2(gross - row_tds)
                        row.save(update_fields=["tds_deducted", "net_amount"])
                    else:
                        row = MilestoneRecord.objects.select_for_update().get(pk=row.pk)
                        row.tds_deducted = row_tds
                        gross = row.bonus_amount or ZERO
                        row.net_bonus = gross if is_194r else _q2(gross - row_tds)
                        row.save(update_fields=["tds_deducted", "net_bonus"])

            wallet.save(
                update_fields=[
                    "cash_balance",
                    "total_tds_deducted",
                    "tds_payable",
                    "updated_at",
                ]
            )
            write_audit(
                "tds.recompute_and_recredit",
                target_type="User",
                target_id=str(user.pk),
                payload={
                    "member_id": user.member_id,
                    "fy": fy,
                    "section": section,
                    "correct_gross": str(alloc.correct_gross),
                    "correct_tds": str(alloc.correct_tds),
                    "recredit_or_payable": str(recredit_or_payable),
                    "rows_normalized": rows_need_normalization,
                },
            )

    @staticmethod
    def _row_net_needs_update(
        kind: str, row, row_tds: Decimal, is_194r: bool
    ) -> bool:
        if kind == "commission":
            gross = row.amount or ZERO
            current = _q2(row.net_amount or ZERO)
        else:
            gross = row.bonus_amount or ZERO
            current = _q2(row.net_bonus or ZERO)
        expected = _q2(gross) if is_194r else _q2(gross - row_tds)
        return current != expected

    def _sync_wallet_total_earned(
        self,
        user: User,
        *,
        fy_start: datetime,
        dry_run: bool,
        apply: bool,
    ) -> None:
        """
        After both section recomputes, ensure wallet.total_earned reflects
        gross of *all* credited commissions + milestones (cash + slot bands).
        """
        all_comm_gross = _q2(
            CommissionLedger.objects.filter(
                recipient_id=user.pk,
                status=CommissionLedger.Status.CREDITED,
                created_at__gte=fy_start,
            ).aggregate(s=Sum("amount"))["s"]
            or ZERO
        )
        all_ms_gross = _q2(
            MilestoneRecord.objects.filter(
                user_id=user.pk,
                status="CREDITED",
                created_at__gte=fy_start,
            ).aggregate(s=Sum("bonus_amount"))["s"]
            or ZERO
        )
        wallet_total_earned = _q2(all_comm_gross + all_ms_gross)

        wallet = Wallet.objects.filter(user_id=user.pk).first()
        if wallet is None:
            return
        if _q2(wallet.total_earned) == wallet_total_earned:
            return
        if dry_run and not apply:
            return
        with transaction.atomic():
            wallet = Wallet.objects.select_for_update().get(user_id=user.pk)
            wallet.total_earned = wallet_total_earned
            wallet.current_band = _band_index_for_earnings(wallet.total_earned)
            wallet.save(
                update_fields=["total_earned", "current_band", "updated_at"]
            )
            on_total_earned_updated(wallet)
