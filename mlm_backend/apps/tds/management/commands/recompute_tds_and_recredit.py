"""
Recompute FY TdsLedger from credited commission/milestone rows (cash bands only)
and re-credit members who were over-withheld due to the false 20k trigger.

Usage:
  python manage.py recompute_tds_and_recredit --fy 2026-27 --dry-run
  python manage.py recompute_tds_and_recredit --fy 2026-27 --apply
"""

from __future__ import annotations

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
    compute_correct_tds_for_cumulative_gross,
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


class Command(BaseCommand):
    help = "Recompute TDS FY ledger and re-credit over-withheld amounts."

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

        self.stdout.write(f"FY={fy}  dry_run={options['dry_run']}  apply={options['apply']}")
        self.stdout.write(
            "member_id | before_ledger_earned | after_ledger_earned | "
            "before_tds | after_tds | recredit | triggered_after"
        )

        for user in users.iterator():
            self._process_user(
                user,
                fy=fy,
                fy_start=fy_start,
                dry_run=options["dry_run"],
                apply=options["apply"],
            )

    def _process_user(
        self,
        user: User,
        *,
        fy: str,
        fy_start: datetime,
        dry_run: bool,
        apply: bool,
    ) -> None:
        comm_qs = CommissionLedger.objects.filter(
            recipient_id=user.pk,
            status=CommissionLedger.Status.CREDITED,
            created_at__gte=fy_start,
            slot_band_held=False,
        )
        ms_qs = MilestoneRecord.objects.filter(
            user_id=user.pk,
            status="CREDITED",
            created_at__gte=fy_start,
            slot_band_held=False,
        )

        correct_gross = _q2(
            (comm_qs.aggregate(s=Sum("amount"))["s"] or ZERO)
            + (ms_qs.aggregate(s=Sum("bonus_amount"))["s"] or ZERO)
        )
        actually_withheld = _q2(
            (comm_qs.aggregate(s=Sum("tds_deducted"))["s"] or ZERO)
            + (ms_qs.aggregate(s=Sum("tds_deducted"))["s"] or ZERO)
        )
        correct_tds = compute_correct_tds_for_cumulative_gross(
            user=user, cumulative_gross=correct_gross
        )
        over_withheld = _q2(max(ZERO, actually_withheld - correct_tds))

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

        ledger = TdsLedger.objects.filter(user_id=user.pk, financial_year=fy).first()
        before_ledger_earned = _q2(ledger.total_earned if ledger else ZERO)
        before_ledger_tds = _q2(ledger.total_tds if ledger else ZERO)

        if (
            before_ledger_earned == correct_gross
            and before_ledger_tds == correct_tds
            and over_withheld <= ZERO
        ):
            return

        correction_ref = f"TDS-CORRECTION-{fy}"
        already_corrected = WalletTransaction.objects.filter(
            user_id=user.pk,
            reference=correction_ref,
        ).exists()

        triggered_after = correct_gross > TDS_THRESHOLD and correct_tds > ZERO
        self.stdout.write(
            f"{user.member_id} | {before_ledger_earned} | {correct_gross} | "
            f"{before_ledger_tds} | {correct_tds} | {over_withheld} | {triggered_after}"
        )

        if dry_run and not apply:
            return

        with transaction.atomic():
            wallet, _ = Wallet.objects.select_for_update().get_or_create(user=user)
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
            ledger.total_earned = correct_gross
            ledger.total_tds = correct_tds
            ledger.tds_triggered = correct_tds > ZERO
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

            wallet.total_earned = wallet_total_earned
            if over_withheld > ZERO and not already_corrected:
                wallet.cash_balance = _q2((wallet.cash_balance or ZERO) + over_withheld)
                wallet.total_tds_deducted = _q2(
                    max(ZERO, (wallet.total_tds_deducted or ZERO) - over_withheld)
                )
                WalletTransaction.objects.create(
                    user=user,
                    tx_type=WalletTransaction.TxType.ADJUSTMENT,
                    amount=over_withheld,
                    balance_after=wallet.cash_balance,
                    reference=correction_ref,
                    meta={
                        "kind": "tds_overwithholding_reversal",
                        "fy": fy,
                        "reason": "recompute",
                        "before_ledger_earned": str(before_ledger_earned),
                        "after_ledger_earned": str(correct_gross),
                        "before_ledger_tds": str(before_ledger_tds),
                        "after_ledger_tds": str(correct_tds),
                    },
                )
            wallet.current_band = _band_index_for_earnings(wallet.total_earned)
            wallet.save(
                update_fields=[
                    "cash_balance",
                    "total_earned",
                    "total_tds_deducted",
                    "current_band",
                    "updated_at",
                ]
            )
            on_total_earned_updated(wallet)

            write_audit(
                "tds.recompute_and_recredit",
                target_type="User",
                target_id=str(user.pk),
                payload={
                    "member_id": user.member_id,
                    "fy": fy,
                    "correct_gross": str(correct_gross),
                    "correct_tds": str(correct_tds),
                    "over_withheld_recredited": str(over_withheld),
                    "already_corrected": already_corrected,
                },
            )
