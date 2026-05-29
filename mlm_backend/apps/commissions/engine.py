import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.admin_panel.utils import get_system_config
from apps.audit.services import write_audit
from apps.payments.models import Order
from apps.users.models import User
from apps.wallet.bands import iter_band_split_pieces, on_total_earned_updated
from apps.wallet.models import Wallet, WalletTransaction
from apps.tds.services import (
    calculate_and_apply_194h_tds,
    calculate_and_apply_194r_tds,
    reverse_194h_tds,
    reverse_194r_tds,
)
from apps.wallet.tds_settlement import settle_tds_payable

from .credit_helpers import tds_wallet_meta, write_commission_wallet_entries
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

        # Binary uplines: 3 passive credits excluding the sponsor.
        # Important: when the sponsor appears in the binary parent chain, we skip
        # that recipient without consuming a passive credit slot.
        node = buyer_node.parent
        passive_types = [
            CommissionLedger.CommissionType.UPLINE_L1,
            CommissionLedger.CommissionType.UPLINE_L2,
            CommissionLedger.CommissionType.UPLINE_L3,
        ]
        credits_given = 0
        while node and credits_given < 3:
            u = node.user
            if u.id != sponsor.id:
                CommissionEngine._credit_user(
                    recipient=u,
                    source=buyer,
                    order=order,
                    ctype=passive_types[credits_given],
                    gross=upline_amt,
                    cap=cap,
                )
                credits_given += 1
            node = node.parent

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
    def _apply_commission_piece(
        *,
        wallet: Wallet,
        recipient: User,
        source: User,
        order: Order,
        ctype: str,
        piece: Decimal,
        slot_band_held: bool,
        ref_suffix: str,
    ) -> None:
        ref_credit = f"COMM-{order.order_number}{ref_suffix}"
        ref_tds = f"TDS-COMM-{order.order_number}{ref_suffix}"
        if slot_band_held:
            r = calculate_and_apply_194r_tds(user=recipient, gross_amount=piece)
            wallet.total_earned += r.gross_amount
            wallet.tds_payable = (wallet.tds_payable or Decimal("0")) + r.tds_amount
            wallet.save()
            CommissionLedger.objects.create(
                recipient=recipient,
                source_user=source,
                order=order,
                commission_type=ctype,
                amount=r.gross_amount,
                tds_deducted=r.tds_amount,
                net_amount=r.gross_amount,
                status=CommissionLedger.Status.CREDITED,
                slot_band_held=True,
            )
            logger.info(
                "commission_credited_slot_band order_id=%s recipient_id=%s type=%s gross=%s tds_194r=%s",
                order.id,
                recipient.id,
                ctype,
                r.gross_amount,
                r.tds_amount,
            )
        else:
            tds = calculate_and_apply_194h_tds(user=recipient, gross_amount=piece)
            wallet.total_earned += tds.gross_amount
            wallet.total_tds_deducted += tds.tds_amount
            write_commission_wallet_entries(
                wallet=wallet,
                recipient=recipient,
                gross=tds.gross_amount,
                tds=tds.tds_amount,
                ref_credit=ref_credit,
                ref_tds=ref_tds,
                credit_meta={
                    "type": ctype,
                    "gross": str(tds.gross_amount),
                    "financial_year": tds.financial_year,
                },
                tds_meta=tds_wallet_meta(
                    tds,
                    extra={"type": ctype, "linked_reference": ref_credit},
                ),
            )
            settle_tds_payable(
                wallet=wallet,
                recipient=recipient,
                reference=f"TDS-194R-SETTLE-{order.order_number}{ref_suffix}",
                defer_save=True,
            )
            wallet.save()
            CommissionLedger.objects.create(
                recipient=recipient,
                source_user=source,
                order=order,
                commission_type=ctype,
                amount=tds.gross_amount,
                tds_deducted=tds.tds_amount,
                net_amount=tds.net_amount,
                status=CommissionLedger.Status.CREDITED,
                slot_band_held=False,
            )
            logger.info(
                "commission_credited order_id=%s recipient_id=%s type=%s gross=%s net=%s",
                order.id,
                recipient.id,
                ctype,
                tds.gross_amount,
                tds.net_amount,
            )

    @staticmethod
    def _apply_milestone_piece(
        *,
        wallet: Wallet,
        sponsor: User,
        threshold: int,
        piece: Decimal,
        slot_band_held: bool,
        ref_suffix: str,
    ) -> None:
        ref_credit = f"MILESTONE-{threshold}{ref_suffix}"
        ref_tds = f"TDS-MILESTONE-{threshold}{ref_suffix}"
        if slot_band_held:
            r = calculate_and_apply_194r_tds(user=sponsor, gross_amount=piece)
            wallet.total_earned += r.gross_amount
            wallet.tds_payable = (wallet.tds_payable or Decimal("0")) + r.tds_amount
            wallet.save()
            MilestoneRecord.objects.create(
                user=sponsor,
                milestone_referrals=threshold,
                bonus_amount=r.gross_amount,
                tds_deducted=r.tds_amount,
                net_bonus=r.gross_amount,
                status="CREDITED",
                slot_band_held=True,
            )
        else:
            tds = calculate_and_apply_194h_tds(user=sponsor, gross_amount=piece)
            wallet.total_earned += tds.gross_amount
            wallet.total_tds_deducted += tds.tds_amount
            write_commission_wallet_entries(
                wallet=wallet,
                recipient=sponsor,
                gross=tds.gross_amount,
                tds=tds.tds_amount,
                ref_credit=ref_credit,
                ref_tds=ref_tds,
                credit_meta={
                    "type": "MILESTONE",
                    "gross": str(tds.gross_amount),
                    "financial_year": tds.financial_year,
                },
                tds_meta=tds_wallet_meta(
                    tds,
                    extra={
                        "type": "MILESTONE",
                        "linked_reference": ref_credit,
                    },
                ),
            )
            settle_tds_payable(
                wallet=wallet,
                recipient=sponsor,
                reference=f"TDS-194R-SETTLE-MILESTONE-{threshold}{ref_suffix}",
                defer_save=True,
            )
            wallet.save()
            MilestoneRecord.objects.create(
                user=sponsor,
                milestone_referrals=threshold,
                bonus_amount=tds.gross_amount,
                tds_deducted=tds.tds_amount,
                net_bonus=tds.net_amount,
                status="CREDITED",
                slot_band_held=False,
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
        if not User.objects.filter(pk=recipient.pk, kyc_first_approved_at__isnull=False).exists():
            # Hold only for users who have never been admin-approved. Previously approved
            # members keep earning during KYC re-review; withdrawals stay blocked separately.
            # Query DB directly so stale cached User rows on binary nodes do not mis-hold.
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
        piece_idx = 0
        for piece, slot_band_held in iter_band_split_pieces(
            total_earned=wallet.total_earned,
            gross=gross_credit,
            cap=cap,
        ):
            piece_idx += 1
            suffix = "" if piece_idx == 1 else f"-S{piece_idx}"
            CommissionEngine._apply_commission_piece(
                wallet=wallet,
                recipient=recipient,
                source=source,
                order=order,
                ctype=ctype,
                piece=piece,
                slot_band_held=slot_band_held,
                ref_suffix=suffix,
            )
            on_total_earned_updated(wallet)
        if wallet.total_earned >= cap:
            recipient.account_status = User.AccountStatus.CAPPED
            recipient.save(update_fields=["account_status"])

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
            if not User.objects.filter(pk=sponsor.pk, kyc_first_approved_at__isnull=False).exists():
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
            piece_idx = 0
            for piece, slot_band_held in iter_band_split_pieces(
                total_earned=wallet.total_earned,
                gross=gross_pay,
                cap=cfg.earning_cap,
            ):
                piece_idx += 1
                suffix = "" if piece_idx == 1 else f"-S{piece_idx}"
                CommissionEngine._apply_milestone_piece(
                    wallet=wallet,
                    sponsor=sponsor,
                    threshold=threshold,
                    piece=piece,
                    slot_band_held=slot_band_held,
                    ref_suffix=suffix,
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
            if e.slot_band_held:
                # Slot-band 194R: gross never hit cash, TDS sits in tds_payable
                # (unless already settled by a later cash event). Reverse the
                # 194R ledger and unwind tds_payable up to the remaining accrual.
                reverse_194r_tds(
                    user=e.recipient,
                    gross_amount=e.amount,
                    tds_amount=e.tds_deducted,
                )
                payable_dec = min(
                    wallet.tds_payable or Decimal("0"), e.tds_deducted
                )
                wallet.tds_payable = (
                    wallet.tds_payable or Decimal("0")
                ) - payable_dec
            else:
                wallet.cash_balance -= e.net_amount
                reverse_194h_tds(
                    user=e.recipient,
                    gross_amount=e.amount,
                    tds_amount=e.tds_deducted,
                )
                wallet.total_tds_deducted = max(
                    Decimal("0"), wallet.total_tds_deducted - e.tds_deducted
                )
            wallet.total_earned -= e.amount
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
