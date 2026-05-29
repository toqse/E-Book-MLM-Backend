"""Model A: gross CREDIT + separate TDS wallet row; FY trigger fixes."""

from decimal import Decimal

import pytest
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from apps.agreements.models import MemberComplianceProfile
from apps.commissions.engine import CommissionEngine
from apps.commissions.models import CommissionLedger
from apps.mlm_tree.services import BinaryTreeService
from apps.payments.models import Order
from apps.tds.models import TdsLedger
from apps.tds.services import (
    TDS_THRESHOLD,
    calculate_and_apply_194h_tds,
    get_current_financial_year,
    reverse_194h_tds,
)
from apps.users.models import User
from apps.users.services import allocate_member_identity
from tests.conftest import unique_test_pan
from apps.wallet.models import Wallet, WalletTransaction


def _verified_sponsor_with_tree():
    mid_r, r_r, l_r = allocate_member_identity()
    root = User(
        phone="+918100000001",
        full_name="Root",
        member_id=mid_r,
        referral_code=r_r,
        referral_link=l_r,
        pan_number=unique_test_pan(),
        kyc_status=User.KYCStatus.VERIFIED,
    )
    root.set_unusable_password()
    root.save()
    BinaryTreeService.place_member(root, None)

    mid_s, r_s, l_s = allocate_member_identity()
    sponsor = User(
        phone="+918100000002",
        full_name="Sponsor",
        member_id=mid_s,
        referral_code=r_s,
        referral_link=l_s,
        sponsor=root,
        pan_number=unique_test_pan(),
        kyc_status=User.KYCStatus.VERIFIED,
    )
    sponsor.set_unusable_password()
    sponsor.save()
    BinaryTreeService.place_member(sponsor, root)

    mid_b, r_b, l_b = allocate_member_identity()
    buyer = User(
        phone="+918100000003",
        full_name="Buyer",
        member_id=mid_b,
        referral_code=r_b,
        referral_link=l_b,
        sponsor=sponsor,
        pan_number=unique_test_pan(),
        kyc_status=User.KYCStatus.VERIFIED,
    )
    buyer.set_unusable_password()
    buyer.save()
    BinaryTreeService.place_member(buyer, sponsor)
    return sponsor, buyer


def _paid_order(buyer: User, order_number: str) -> Order:
    now = timezone.now()
    return Order.objects.create(
        user=buyer,
        order_number=order_number,
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("241.72"),
        is_retail_purchase=False,
        status=Order.Status.PAID,
        paid_at=now,
        refund_eligible_until=now - timezone.timedelta(days=1),
    )


@pytest.mark.django_db
def test_commission_credit_writes_two_wallet_rows_when_tds_applies(system_config):
    sponsor, buyer = _verified_sponsor_with_tree()
    calculate_and_apply_194h_tds(user=sponsor, gross_amount=TDS_THRESHOLD)
    order = _paid_order(buyer, "ORD-TDS-TWO-ROWS")
    CommissionEngine.process_order(order)

    assert WalletTransaction.objects.filter(
        user=sponsor,
        tx_type=WalletTransaction.TxType.CREDIT,
        reference=f"COMM-{order.order_number}",
    ).exists()
    assert WalletTransaction.objects.filter(
        user=sponsor,
        tx_type=WalletTransaction.TxType.TDS,
        reference=f"TDS-COMM-{order.order_number}",
    ).exists()

    credit = CommissionLedger.objects.filter(recipient=sponsor, order=order).first()
    assert credit.tds_deducted > 0
    credit_tx = WalletTransaction.objects.get(
        user=sponsor,
        tx_type=WalletTransaction.TxType.CREDIT,
        reference=f"COMM-{order.order_number}",
    )
    tds_tx = WalletTransaction.objects.get(
        user=sponsor,
        tx_type=WalletTransaction.TxType.TDS,
        reference=f"TDS-COMM-{order.order_number}",
    )
    assert credit_tx.amount == credit.amount
    assert tds_tx.amount == credit.tds_deducted


@pytest.mark.django_db
def test_commission_credit_writes_single_wallet_row_when_tds_zero(system_config):
    sponsor, buyer = _verified_sponsor_with_tree()
    order = _paid_order(buyer, "ORD-TDS-ZERO")
    CommissionEngine.process_order(order)

    assert WalletTransaction.objects.filter(
        user=sponsor,
        tx_type=WalletTransaction.TxType.CREDIT,
    ).count() >= 1
    assert not WalletTransaction.objects.filter(
        user=sponsor,
        tx_type=WalletTransaction.TxType.TDS,
    ).exists()


@pytest.mark.django_db
def test_withdrawal_does_not_touch_tds_ledger(system_config):
    from apps.admin_panel.models import SystemConfig

    SystemConfig.objects.filter(pk=1).update(cooling_off_days=0)
    sponsor, buyer = _verified_sponsor_with_tree()
    order = _paid_order(buyer, "ORD-WD-NO-TDS")
    CommissionEngine.process_order(order)
    w = Wallet.objects.get(user=sponsor)
    before = TdsLedger.objects.filter(user=sponsor).first()
    before_earned = before.total_earned if before else Decimal("0")

    MemberComplianceProfile.objects.get_or_create(user=sponsor)
    sponsor.bank_account_number = "123456789012"
    sponsor.bank_ifsc = "HDFC0001234"
    sponsor.upi_id = "sponsor@okhdfcbank"
    sponsor.save()
    Wallet.objects.filter(user=sponsor).update(cash_balance=Decimal("500"), current_band=1)
    client = APIClient()
    client.force_authenticate(user=sponsor)
    r = client.post(
        "/api/v1/user/wallet/withdraw/",
        {"amount": "200", "band": 1, "payout_method": "BANK"},
        format="json",
    )
    assert r.status_code == 200
    assert r.json()["data"]["tds_amount"] == "0.00"
    assert r.json()["data"]["net_payable"] == "200.00"

    after = TdsLedger.objects.get(user=sponsor)
    assert after.total_earned == before_earned


@pytest.mark.django_db
def test_commission_reversal_unwinds_tds_ledger(system_config):
    sponsor, buyer = _verified_sponsor_with_tree()
    order = _paid_order(buyer, "ORD-REV-TDS")
    CommissionEngine.process_order(order)
    entry = CommissionLedger.objects.get(recipient=sponsor, order=order)
    ledger_before = TdsLedger.objects.get(user=sponsor, financial_year=get_current_financial_year())

    CommissionEngine.reverse_commissions(order)
    ledger_after = TdsLedger.objects.get(user=sponsor, financial_year=get_current_financial_year())
    assert ledger_after.total_earned == ledger_before.total_earned - entry.amount
    assert ledger_after.total_tds == ledger_before.total_tds - entry.tds_deducted


@pytest.mark.django_db
def test_wallet_total_earned_tracks_gross_after_credit(system_config):
    sponsor, buyer = _verified_sponsor_with_tree()
    order = _paid_order(buyer, "ORD-GROSS-EARNED")
    CommissionEngine.process_order(order)
    entry = CommissionLedger.objects.get(recipient=sponsor, order=order)
    w = Wallet.objects.get(user=sponsor)
    assert w.total_earned == entry.amount


@pytest.mark.django_db
def test_reverse_194h_tds_resets_trigger_when_below_threshold(system_config):
    mid, ref, link = allocate_member_identity()
    u = User(
        phone="+918199999999",
        full_name="T",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        pan_number=unique_test_pan(),
    )
    u.set_unusable_password()
    u.save()
    calculate_and_apply_194h_tds(user=u, gross_amount=Decimal("20010"))
    reverse_194h_tds(user=u, gross_amount=Decimal("20010"), tds_amount=Decimal("400.20"))
    ledger = TdsLedger.objects.get(user=u, financial_year=get_current_financial_year())
    assert ledger.tds_triggered is False
    assert ledger.total_earned == Decimal("0.00")


@pytest.mark.django_db
def test_recompute_tds_and_recredit_idempotent(system_config):
    mid, ref, link = allocate_member_identity()
    u = User(
        phone="+918188888888",
        full_name="Recredit",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        pan_number=unique_test_pan(),
        kyc_status=User.KYCStatus.VERIFIED,
    )
    u.set_unusable_password()
    u.save()
    buyer_mid, buyer_ref, buyer_link = allocate_member_identity()
    buyer = User(
        phone="+918188888887",
        full_name="Buyer",
        member_id=buyer_mid,
        referral_code=buyer_ref,
        referral_link=buyer_link,
        sponsor=u,
    )
    buyer.set_unusable_password()
    buyer.save()
    order = Order.objects.create(
        user=buyer,
        order_number="ORD-RECREDIT",
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("241.72"),
        is_retail_purchase=False,
        status=Order.Status.PAID,
        paid_at=timezone.now(),
    )
    CommissionLedger.objects.create(
        recipient=u,
        source_user=buyer,
        order=order,
        commission_type=CommissionLedger.CommissionType.DIRECT,
        amount=Decimal("30"),
        tds_deducted=Decimal("30"),
        net_amount=Decimal("0"),
        status=CommissionLedger.Status.CREDITED,
        slot_band_held=False,
    )
    fy = get_current_financial_year()
    TdsLedger.objects.create(
        user=u,
        financial_year=fy,
        total_earned=Decimal("25000"),
        total_tds=Decimal("500"),
        tds_triggered=True,
    )
    Wallet.objects.create(
        user=u,
        cash_balance=Decimal("100"),
        total_earned=Decimal("30"),
        total_tds_deducted=Decimal("30"),
    )

    call_command("recompute_tds_and_recredit", f"--fy={fy}", "--apply")
    w1 = Wallet.objects.get(user=u)
    bal1 = w1.cash_balance
    assert bal1 == Decimal("130.00")
    entry = CommissionLedger.objects.get(recipient=u, order=order)
    assert entry.tds_deducted == Decimal("0.00")
    assert entry.net_amount == Decimal("30.00")

    call_command("recompute_tds_and_recredit", f"--fy={fy}", "--apply")
    w2 = Wallet.objects.get(user=u)
    assert w2.cash_balance == bal1
    assert WalletTransaction.objects.filter(user=u, reference=f"TDS-CORRECTION-{fy}").count() == 1
