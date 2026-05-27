"""
Reconcile (relabel + backfill) binary upline passive commissions.

Purpose:
1) After the engine payout rule change (count passive credits excluding
   sponsor without consuming a slot), some historic orders may be missing
   the last passive credit.
2) After the rename UPLINE_L2/UPLINE_L3/UPLINE_L4 -> UPLINE_L1/UPLINE_L2/UPLINE_L3,
   existing passive ledger rows may have incorrect `commission_type` labels.

This command is idempotent:
- It only creates a missing passive credit if there is no non-reversed passive
  ledger row for (order, recipient, source_user).
- It only updates `commission_type` (no wallet/timing mutation for existing
  rows).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import QuerySet

from apps.admin_panel.utils import get_system_config
from apps.commissions.engine import CommissionEngine
from apps.commissions.models import CommissionLedger
from apps.payments.models import Order


PASSIVE_TYPE_VALUES = ("UPLINE_L1", "UPLINE_L2", "UPLINE_L3", "UPLINE_L4")


@dataclass(frozen=True)
class ExpectedPassive:
    recipient_id: int
    commission_type: str


def _expected_passive_credits(*, buyer, sponsor, buyer_node) -> list[ExpectedPassive]:
    """
    Compute expected 3 passive credits for this order using the new rule:
    - walk buyer_node.parent upward
    - skip sponsor without consuming a passive slot
    """

    passive_types = [
        CommissionLedger.CommissionType.UPLINE_L1,
        CommissionLedger.CommissionType.UPLINE_L2,
        CommissionLedger.CommissionType.UPLINE_L3,
    ]

    node = buyer_node.parent
    credits_given = 0
    expected: list[ExpectedPassive] = []
    while node and credits_given < 3:
        u = node.user
        if u.id != sponsor.id:
            expected.append(
                ExpectedPassive(
                    recipient_id=u.id,
                    commission_type=passive_types[credits_given],
                )
            )
            credits_given += 1
        node = node.parent
    return expected


def _non_reversed_passive_exists(*, order_id: int, source_user_id: int, recipient_id: int) -> bool:
    return CommissionLedger.objects.filter(
        order_id=order_id,
        source_user_id=source_user_id,
        recipient_id=recipient_id,
        status__in=(CommissionLedger.Status.CREDITED, CommissionLedger.Status.HELD, CommissionLedger.Status.PENDING),
        commission_type__in=PASSIVE_TYPE_VALUES,
    ).exists()


class Command(BaseCommand):
    help = (
        "Reconcile upline passive commissions: relabel existing passive ledger rows "
        "and backfill any missing passive credits after the payout rule rename."
    )

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--dry-run", action="store_true", help="Simulate without writing.")
        group.add_argument("--apply", action="store_true", help="Relabel and backfill.")
        parser.add_argument("--order-id", type=int, default=None, help="Limit to one order id.")
        parser.add_argument("--user-id", type=int, default=None, help="Limit to one buyer user id.")

    def handle(self, *args: Any, **options: Any):
        apply = bool(options["apply"])
        dry_run = bool(options["dry_run"])
        assert dry_run != apply

        order_id = options.get("order_id")
        user_id = options.get("user_id")
        cfg = get_system_config()
        User = get_user_model()

        qs: QuerySet[Order] = Order.objects.filter(
            status=Order.Status.PAID,
            is_retail_purchase=False,
        ).select_related("user", "user__sponsor", "user__binary_node")

        if order_id is not None:
            qs = qs.filter(id=order_id)
        if user_id is not None:
            qs = qs.filter(user_id=user_id)

        # Process in deterministic order to keep cap usage behavior stable-ish.
        qs = qs.order_by("paid_at", "created_at", "id")

        stats = {
            "orders_scanned": 0,
            "orders_had_expected": 0,
            "relabel_updates": 0,
            "passive_credits_created": 0,
            "passive_credits_would_create": 0,
            "already_present_skipped": 0,
        }

        for order in qs.iterator():
            stats["orders_scanned"] += 1
            buyer = order.user
            sponsor = getattr(buyer, "sponsor", None)
            if not sponsor:
                continue

            try:
                buyer_node = buyer.binary_node
            except Exception:
                continue

            expected = _expected_passive_credits(
                buyer=buyer,
                sponsor=sponsor,
                buyer_node=buyer_node,
            )
            if not expected:
                continue

            stats["orders_had_expected"] += 1
            expected_by_recipient = {e.recipient_id: e for e in expected}

            with transaction.atomic():
                # (1) Relabel existing passive rows for expected recipients.
                if apply:
                    for recipient_id, e in expected_by_recipient.items():
                        rows = CommissionLedger.objects.filter(
                            order_id=order.id,
                            source_user_id=buyer.id,
                            recipient_id=recipient_id,
                            status__in=(
                                CommissionLedger.Status.CREDITED,
                                CommissionLedger.Status.HELD,
                                CommissionLedger.Status.PENDING,
                            ),
                            commission_type__in=PASSIVE_TYPE_VALUES,
                        )
                        if rows.exists():
                            updated = rows.update(commission_type=e.commission_type)
                            stats["relabel_updates"] += updated

                # (2) Backfill any missing passive credit.
                upline_amt = cfg.upline_commission
                cap = cfg.earning_cap
                for expected_rec in expected:
                    if _non_reversed_passive_exists(
                        order_id=order.id,
                        source_user_id=buyer.id,
                        recipient_id=expected_rec.recipient_id,
                    ):
                        stats["already_present_skipped"] += 1
                        continue

                    if not apply:
                        stats["passive_credits_would_create"] += 1
                        continue

                    recipient = User.objects.get(pk=expected_rec.recipient_id)
                    CommissionEngine._credit_user(
                        recipient=recipient,
                        source=buyer,
                        order=order,
                        ctype=expected_rec.commission_type,
                        gross=upline_amt,
                        cap=cap,
                    )
                    stats["passive_credits_created"] += 1

        self.stdout.write(self.style.SUCCESS(f"Reconcile complete: {stats}"))

