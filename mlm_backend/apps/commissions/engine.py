from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.admin_panel.utils import get_system_config
from apps.audit.services import write_audit
from apps.payments.models import Order
from apps.users.models import User
from apps.wallet.bands import on_total_earned_updated
from apps.wallet.models import Wallet, WalletTransaction

from .models import CommissionLedger, MilestoneRecord

MILESTONES = [
    (10, Decimal("0.15"), Decimal("300")),
    (25, Decimal("0.12"), Decimal("600")),
    (50, Decimal("0.10"), Decimal("1000")),
    (75, Decimal("0.09"), Decimal("1350")),
    (100, Decimal("0.08"), Decimal("1600")),
]


class CommissionEngine:
    @staticmethod
    @transaction.atomic
    def process_order(order: Order) -> None:
        if order.status != Order.Status.PAID:
            return
        buyer = order.user
        cfg = get_system_config()
        direct_amt = cfg.direct_commission
        upline_amt = cfg.upline_commission
        cap = cfg.earning_cap

        if order.is_retail_purchase:
            write_audit(
                "order.commission_skipped_retail",
                actor=None,
                target_type="Order",
                target_id=order.id,
                payload={"order_number": order.order_number},
            )
            return

        sponsor = buyer.sponsor
        if not sponsor:
            write_audit(
                "order.no_sponsor",
                target_type="Order",
                target_id=order.id,
            )
            return

        if not hasattr(buyer, "binary_node"):
            return

        buyer_node = buyer.binary_node

        # Direct commission to sponsor
        CommissionEngine._credit_user(
            recipient=sponsor,
            source=buyer,
            order=order,
            ctype=CommissionLedger.CommissionType.DIRECT,
            gross=direct_amt,
            cap=cap,
        )

        sponsor.direct_referral_count = (sponsor.direct_referral_count or 0) + 1
        sponsor.save(update_fields=["direct_referral_count"])
        CommissionEngine._maybe_milestone(sponsor, cfg)

        # Binary uplines: parent chain up to 3, skip sponsor
        node = buyer_node.parent
        hops = 0
        while node and hops < 3:
            u = node.user
            if u.id != sponsor.id:
                CommissionEngine._credit_user(
                    recipient=u,
                    source=buyer,
                    order=order,
                    ctype=[
                        CommissionLedger.CommissionType.UPLINE_L2,
                        CommissionLedger.CommissionType.UPLINE_L3,
                        CommissionLedger.CommissionType.UPLINE_L4,
                    ][hops],
                    gross=upline_amt,
                    cap=cap,
                )
            node = node.parent
            hops += 1

        write_audit(
            "order.commissions_processed",
            target_type="Order",
            target_id=order.id,
            payload={"order_number": order.order_number},
        )

    @staticmethod
    def _credit_user(
        recipient: User,
        source: User,
        order: Order,
        ctype: str,
        gross: Decimal,
        cap: Decimal,
    ):
        wallet, _ = Wallet.objects.select_for_update().get_or_create(user=recipient)
        remaining = cap - wallet.total_earned
        if remaining <= 0:
            recipient.account_status = User.AccountStatus.CAPPED
            recipient.save(update_fields=["account_status"])
            CommissionLedger.objects.create(
                recipient=recipient,
                source_user=source,
                order=order,
                commission_type=ctype,
                amount=gross,
                tds_deducted=Decimal("0"),
                net_amount=Decimal("0"),
                status=CommissionLedger.Status.HELD,
            )
            return
        credit = min(gross, remaining)
        wallet.cash_balance += credit
        wallet.total_earned += credit
        wallet.save()
        WalletTransaction.objects.create(
            user=recipient,
            tx_type=WalletTransaction.TxType.CREDIT,
            amount=credit,
            balance_after=wallet.cash_balance,
            reference=f"COMM-{order.order_number}",
            meta={"type": ctype},
        )
        CommissionLedger.objects.create(
            recipient=recipient,
            source_user=source,
            order=order,
            commission_type=ctype,
            amount=gross,
            tds_deducted=Decimal("0"),
            net_amount=credit,
            status=CommissionLedger.Status.CREDITED,
        )
        if wallet.total_earned >= cap:
            recipient.account_status = User.AccountStatus.CAPPED
            recipient.save(update_fields=["account_status"])
        on_total_earned_updated(wallet)

    @staticmethod
    def _maybe_milestone(sponsor: User, cfg):
        count = sponsor.direct_referral_count
        for threshold, _pct, bonus in MILESTONES:
            if count != threshold:
                continue
            if MilestoneRecord.objects.filter(
                user=sponsor, milestone_referrals=threshold
            ).exists():
                continue
            wallet, _ = Wallet.objects.select_for_update().get_or_create(user=sponsor)
            remaining = cfg.earning_cap - wallet.total_earned
            if remaining <= 0:
                return
            pay = min(bonus, remaining)
            wallet.cash_balance += pay
            wallet.total_earned += pay
            wallet.save()
            MilestoneRecord.objects.create(
                user=sponsor,
                milestone_referrals=threshold,
                bonus_amount=bonus,
                tds_deducted=Decimal("0"),
                net_bonus=pay,
                status="CREDITED",
            )
            WalletTransaction.objects.create(
                user=sponsor,
                tx_type=WalletTransaction.TxType.CREDIT,
                amount=pay,
                balance_after=wallet.cash_balance,
                reference=f"MILESTONE-{threshold}",
            )
            on_total_earned_updated(wallet)

    @staticmethod
    @transaction.atomic
    def reverse_commissions(order: Order) -> None:
        entries = CommissionLedger.objects.filter(
            order=order, status=CommissionLedger.Status.CREDITED
        ).select_related("recipient")
        for e in entries:
            wallet = Wallet.objects.select_for_update().get(user=e.recipient)
            wallet.cash_balance -= e.net_amount
            wallet.total_earned -= e.net_amount
            wallet.save()
            WalletTransaction.objects.create(
                user=e.recipient,
                tx_type=WalletTransaction.TxType.DEBIT,
                amount=e.net_amount,
                balance_after=wallet.cash_balance,
                reference=f"REV-{order.order_number}",
            )
            e.status = CommissionLedger.Status.REVERSED
            e.save(update_fields=["status"])
        write_audit(
            "order.commissions_reversed",
            target_type="Order",
            target_id=order.id,
        )
