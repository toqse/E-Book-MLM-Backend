import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.admin_panel.utils import get_system_config
from apps.audit.services import write_audit
from apps.payments.models import Order
from apps.users.models import User
from apps.wallet.bands import (
    SLOT_BAND_NUMBERS,
    _band_index_for_earnings,
    is_slot_band,
    on_total_earned_updated,
)
from apps.wallet.models import Wallet, WalletTransaction
from apps.tds.services import calculate_and_apply_194h_tds

from .milestone_tiers import get_milestones
from .models import CommissionLedger, MilestoneRecord

logger = logging.getLogger(__name__)


class CommissionEngine:
    @staticmethod
    @transaction.atomic
    def process_order(order: Order) -> None:
        logger.info(
            "commission_process_start order_id=%s status=%s order_number=%s",
            order.id,
            order.status,
            order.order_number,
        )
        if order.status != Order.Status.PAID:
            logger.warning(
                "commission_skipped_not_paid order_id=%s status=%s",
                order.id,
                order.status,
            )
            return
        active = CommissionLedger.objects.filter(order=order).exclude(
            status=CommissionLedger.Status.REVERSED
        )
        if active.exists():
            logger.info(
                "commission_skipped_duplicate order_id=%s existing_count=%s",
                order.id,
                active.count(),
            )
            return
        # Purge stale REVERSED rows so they don't pollute the ledger or the
        # "reversed" summary figure after an admin reverse + re-placement cycle.
        CommissionLedger.objects.filter(
            order=order, status=CommissionLedger.Status.REVERSED
        ).delete()
        buyer = order.user
        cfg = get_system_config()
        direct_amt = cfg.direct_commission
        upline_amt = cfg.upline_commission
        cap = cfg.earning_cap

        if order.is_retail_purchase:
            logger.info("commission_skipped_retail order_id=%s", order.id)
            write_audit(
                "order.commission_skipped_retail",
                actor=None,
                target_type="Order",
                target_id=order.id,
                payload={"order_number": order.order_number},
            )
            return

        if not cfg.is_repurchase_commission_allowed:
            prior = Order.objects.filter(
                user_id=buyer.id,
                status=Order.Status.PAID,
                is_retail_purchase=False,
            ).exclude(pk=order.pk)
            if prior.exists():
                logger.info(
                    "commission_skipped_repurchase order_id=%s buyer_id=%s",
                    order.id,
                    buyer.id,
                )
                write_audit(
                    "order.commission_skipped_repurchase",
                    actor=None,
                    target_type="Order",
                    target_id=order.id,
                    payload={"order_number": order.order_number, "buyer_id": buyer.id},
                )
                return

        sponsor = buyer.sponsor
        if not sponsor:
            logger.warning(
                "commission_skipped_no_sponsor order_id=%s buyer_id=%s",
                order.id,
                buyer.id,
            )
            write_audit(
                "order.no_sponsor",
                target_type="Order",
                target_id=order.id,
            )
            return

        if not hasattr(buyer, "binary_node"):
            logger.warning(
                "commission_skipped_no_binary_node order_id=%s buyer_id=%s",
                order.id,
                buyer.id,
            )
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

        logger.info(
            "commission_process_done order_id=%s order_number=%s sponsor_id=%s",
            order.id,
            order.order_number,
            sponsor.id,
        )
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
        if recipient.kyc_status != User.KYCStatus.VERIFIED:
            logger.info(
                "commission_held_kyc order_id=%s recipient_id=%s type=%s",
                order.id,
                recipient.id,
                ctype,
            )
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
        wallet, _ = Wallet.objects.select_for_update().get_or_create(user=recipient)
        remaining = cap - wallet.total_earned
        if remaining <= 0:
            logger.info(
                "commission_held_cap order_id=%s recipient_id=%s type=%s",
                order.id,
                recipient.id,
                ctype,
            )
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
        gross_credit = min(gross, remaining)
        if gross_credit < gross:
            logger.info(
                "commission_partial_cap order_id=%s recipient_id=%s type=%s gross=%s credited=%s",
                order.id,
                recipient.id,
                ctype,
                gross,
                gross_credit,
            )
        tds = calculate_and_apply_194h_tds(user=recipient, gross_amount=gross_credit)
        # Slot-band routing: the band gate is read from total_earned BEFORE the
        # bump, matching how `on_total_earned_updated` issues slot batches at
        # the boundary. A credit that lands fully inside a slot band funds the
        # sponsor-slot mechanism instead of cash.
        band_before_credit = _band_index_for_earnings(wallet.total_earned)
        slot_band_held = band_before_credit in SLOT_BAND_NUMBERS
        if not slot_band_held:
            wallet.cash_balance += tds.net_amount
        wallet.total_earned += tds.net_amount
        wallet.total_tds_deducted += tds.tds_amount
        wallet.save()
        if not slot_band_held:
            WalletTransaction.objects.create(
                user=recipient,
                tx_type=WalletTransaction.TxType.CREDIT,
                amount=tds.net_amount,
                balance_after=wallet.cash_balance,
                reference=f"COMM-{order.order_number}",
                meta={
                    "type": ctype,
                    "gross": str(tds.gross_amount),
                    "tds": str(tds.tds_amount),
                    "tds_rate_percent": str(tds.tds_rate_percent),
                    "financial_year": tds.financial_year,
                },
            )
        CommissionLedger.objects.create(
            recipient=recipient,
            source_user=source,
            order=order,
            commission_type=ctype,
            amount=tds.gross_amount,
            tds_deducted=tds.tds_amount,
            net_amount=tds.net_amount,
            status=CommissionLedger.Status.CREDITED,
            slot_band_held=slot_band_held,
        )
        logger.info(
            "commission_credited order_id=%s recipient_id=%s type=%s gross=%s net=%s slot_band_held=%s",
            order.id,
            recipient.id,
            ctype,
            tds.gross_amount,
            tds.net_amount,
            slot_band_held,
        )
        if wallet.total_earned >= cap:
            recipient.account_status = User.AccountStatus.CAPPED
            recipient.save(update_fields=["account_status"])
        on_total_earned_updated(wallet)

    @staticmethod
    def _maybe_milestone(sponsor: User, cfg):
        count = sponsor.direct_referral_count
        for threshold, _pct, bonus in get_milestones(cfg):
            if count != threshold:
                continue
            if MilestoneRecord.objects.filter(
                user=sponsor, milestone_referrals=threshold
            ).exists():
                continue
            if not getattr(cfg, "auto_process_milestone_bonuses", True):
                MilestoneRecord.objects.create(
                    user=sponsor,
                    milestone_referrals=threshold,
                    bonus_amount=bonus,
                    tds_deducted=Decimal("0"),
                    net_bonus=Decimal("0"),
                    status="PENDING",
                )
                return
            if sponsor.kyc_status != User.KYCStatus.VERIFIED:
                MilestoneRecord.objects.create(
                    user=sponsor,
                    milestone_referrals=threshold,
                    bonus_amount=bonus,
                    tds_deducted=Decimal("0"),
                    net_bonus=Decimal("0"),
                    status="HELD",
                )
                return
            wallet, _ = Wallet.objects.select_for_update().get_or_create(user=sponsor)
            remaining = cfg.earning_cap - wallet.total_earned
            if remaining <= 0:
                return
            gross_pay = min(bonus, remaining)
            tds = calculate_and_apply_194h_tds(user=sponsor, gross_amount=gross_pay)
            band_before_credit = _band_index_for_earnings(wallet.total_earned)
            slot_band_held = band_before_credit in SLOT_BAND_NUMBERS
            if not slot_band_held:
                wallet.cash_balance += tds.net_amount
            wallet.total_earned += tds.net_amount
            wallet.total_tds_deducted += tds.tds_amount
            wallet.save()
            MilestoneRecord.objects.create(
                user=sponsor,
                milestone_referrals=threshold,
                bonus_amount=tds.gross_amount,
                tds_deducted=tds.tds_amount,
                net_bonus=tds.net_amount,
                status="CREDITED",
                slot_band_held=slot_band_held,
            )
            if not slot_band_held:
                WalletTransaction.objects.create(
                    user=sponsor,
                    tx_type=WalletTransaction.TxType.CREDIT,
                    amount=tds.net_amount,
                    balance_after=wallet.cash_balance,
                    reference=f"MILESTONE-{threshold}",
                    meta={
                        "type": "MILESTONE",
                        "gross": str(tds.gross_amount),
                        "tds": str(tds.tds_amount),
                        "tds_rate_percent": str(tds.tds_rate_percent),
                        "financial_year": tds.financial_year,
                    },
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
            # Slot-band-held credits never increased cash_balance, so reversing
            # them must not decrease it either. They still unwind total_earned
            # (which un-funds the slot pool on the lifetime ledger).
            if not e.slot_band_held:
                wallet.cash_balance -= e.net_amount
            wallet.total_earned -= e.net_amount
            wallet.save()
            if not e.slot_band_held:
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
