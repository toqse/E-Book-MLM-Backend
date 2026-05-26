"""Sec 194R (slot-band) TDS accrual, settlement, reversal, and recompute."""

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
    TDS_THRESHOLD_194R,
    calculate_and_apply_194r_tds,
    get_current_financial_year,
)
from apps.users.models import User
from apps.users.services import allocate_member_identity
from apps.wallet.models import Wallet, WalletTransaction
from apps.wallet.tds_settlement import settle_tds_payable


def _make_verified_user(phone: str, *, pan: str | None = "ABCDE1234F") -> User:
    mid, ref, link = allocate_member_identity()
    u = User(
        phone=phone,
        full_name="Test",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        pan_number=pan or "",
        kyc_status=User.KYCStatus.VERIFIED,
    )
    u.set_unusable_password()
    u.save()
    return u


def _make_sponsor_tree_in_slot_band(slot_total_earned: Decimal) -> tuple[User, User]:
    root = _make_verified_user("+918110000001")
    BinaryTreeService.place_member(root, None)

    sponsor = _make_verified_user("+918110000002")
    sponsor.sponsor = root
    sponsor.save()
    BinaryTreeService.place_member(sponsor, root)

    Wallet.objects.update_or_create(
        user=sponsor,
        defaults={
            "total_earned": slot_total_earned,
            "current_band": 2,
            "cash_balance": Decimal("0"),
        },
    )

    buyer = _make_verified_user("+918110000003")
    buyer.sponsor = sponsor
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
def test_194r_calc_under_threshold_returns_zero_tds(system_config):
    u = _make_verified_user("+918111000001")
    r = calculate_and_apply_194r_tds(user=u, gross_amount=Decimal("1000"))
    assert r.tds_amount == Decimal("0.00")
    assert r.net_amount == Decimal("1000.00")
    ledger = TdsLedger.objects.get(
        user=u,
        financial_year=get_current_financial_year(),
        section=TdsLedger.SECTION_194R,
    )
    assert ledger.total_earned == Decimal("1000.00")
    assert ledger.total_tds == Decimal("0.00")
    assert ledger.tds_triggered is False


@pytest.mark.django_db
def test_194r_calc_crossing_threshold_applies_catchup(system_config):
    u = _make_verified_user("+918111000002")
    TdsLedger.objects.create(
        user=u,
        financial_year=get_current_financial_year(),
        section=TdsLedger.SECTION_194R,
        total_earned=TDS_THRESHOLD_194R - Decimal("100"),
        total_tds=Decimal("0"),
        tds_triggered=False,
    )
    # Picked so that catchup TDS does NOT exceed gross (else it gets capped).
    r = calculate_and_apply_194r_tds(user=u, gross_amount=Decimal("5000"))
    # new_total = 24900, rate=10% -> required_total=2490.00, prev_tds=0 -> tds=2490.00
    assert r.tds_amount == Decimal("2490.00")
    assert r.tds_applicable is True


@pytest.mark.django_db
def test_194r_has_independent_threshold_from_194h(system_config):
    u = _make_verified_user("+918111000003")
    # Push the 194H ledger over threshold; that must NOT trigger 194R.
    TdsLedger.objects.create(
        user=u,
        financial_year=get_current_financial_year(),
        section=TdsLedger.SECTION_194H,
        total_earned=TDS_THRESHOLD_194R + Decimal("1000"),
        total_tds=Decimal("420"),
        tds_triggered=True,
    )
    r = calculate_and_apply_194r_tds(user=u, gross_amount=Decimal("500"))
    assert r.tds_amount == Decimal("0.00")
    assert r.tds_applicable is False


@pytest.mark.django_db
def test_slot_band_commission_accrues_tds_payable_no_cash(system_config):
    sponsor, buyer = _make_sponsor_tree_in_slot_band(Decimal("4000"))
    order = _paid_order(buyer, "ORD-194R-SLOT")
    CommissionEngine.process_order(order)

    entry = CommissionLedger.objects.get(recipient=sponsor, order=order)
    assert entry.slot_band_held is True
    assert entry.amount == Decimal("30.00")
    # Under threshold, so accrued TDS is zero on this credit.
    assert entry.tds_deducted == Decimal("0.00")
    assert entry.net_amount == Decimal("30.00")

    w = Wallet.objects.get(user=sponsor)
    # Cash never moved for slot-band credits.
    assert w.cash_balance == Decimal("0.00")
    assert w.tds_payable == Decimal("0.00")
    assert not WalletTransaction.objects.filter(user=sponsor, reference=f"COMM-{order.order_number}").exists()


@pytest.mark.django_db
def test_slot_band_commission_accrues_tds_payable_when_threshold_crossed(system_config):
    sponsor, buyer = _make_sponsor_tree_in_slot_band(Decimal("4000"))
    TdsLedger.objects.create(
        user=sponsor,
        financial_year=get_current_financial_year(),
        section=TdsLedger.SECTION_194R,
        total_earned=TDS_THRESHOLD_194R - Decimal("10"),
        total_tds=Decimal("0"),
        tds_triggered=False,
    )
    order = _paid_order(buyer, "ORD-194R-OVER")
    CommissionEngine.process_order(order)
    entry = CommissionLedger.objects.get(recipient=sponsor, order=order)
    assert entry.slot_band_held is True
    assert entry.tds_deducted > Decimal("0")
    w = Wallet.objects.get(user=sponsor)
    assert w.tds_payable == entry.tds_deducted
    assert w.cash_balance == Decimal("0.00")


@pytest.mark.django_db
def test_settle_tds_payable_debits_cash_and_writes_tds_row(system_config):
    u = _make_verified_user("+918111000010")
    w = Wallet.objects.create(
        user=u,
        cash_balance=Decimal("100"),
        tds_payable=Decimal("40"),
        total_tds_deducted=Decimal("0"),
    )
    settled = settle_tds_payable(wallet=w, recipient=u, reference="TDS-194R-SETTLE-X")
    assert settled == Decimal("40")
    w.refresh_from_db()
    assert w.cash_balance == Decimal("60")
    assert w.tds_payable == Decimal("0")
    assert w.total_tds_deducted == Decimal("40")
    tx = WalletTransaction.objects.get(user=u, reference="TDS-194R-SETTLE-X")
    assert tx.tx_type == WalletTransaction.TxType.TDS
    assert tx.amount == Decimal("40")


@pytest.mark.django_db
def test_settle_tds_payable_partial_when_cash_short(system_config):
    u = _make_verified_user("+918111000011")
    w = Wallet.objects.create(
        user=u,
        cash_balance=Decimal("10"),
        tds_payable=Decimal("40"),
    )
    settled = settle_tds_payable(wallet=w, recipient=u, reference="TDS-194R-SETTLE-P")
    assert settled == Decimal("10")
    w.refresh_from_db()
    assert w.cash_balance == Decimal("0")
    assert w.tds_payable == Decimal("30")


@pytest.mark.django_db(transaction=True)
def test_settle_tds_payable_defer_save_warns_when_not_saved(settings):
    """If a caller passes defer_save=True but forgets to save, on_commit must trip."""
    from django.db import transaction

    settings.DEBUG = True
    u = _make_verified_user("+918111000012")
    w = Wallet.objects.create(
        user=u,
        cash_balance=Decimal("50"),
        tds_payable=Decimal("20"),
    )
    with pytest.raises(AssertionError):
        with transaction.atomic():
            settle_tds_payable(
                wallet=w,
                recipient=u,
                reference="TDS-194R-SETTLE-NOSAVE",
                defer_save=True,
            )
            # Intentionally NOT saving the wallet.


@pytest.mark.django_db(transaction=True)
def test_settle_tds_payable_defer_save_ok_when_caller_saves(settings):
    from django.db import transaction

    settings.DEBUG = True
    u = _make_verified_user("+918111000013")
    w = Wallet.objects.create(
        user=u,
        cash_balance=Decimal("50"),
        tds_payable=Decimal("20"),
    )
    with transaction.atomic():
        settle_tds_payable(
            wallet=w,
            recipient=u,
            reference="TDS-194R-SETTLE-SAVED",
            defer_save=True,
        )
        w.save()
    w.refresh_from_db()
    assert w.cash_balance == Decimal("30")
    assert w.tds_payable == Decimal("0")


@pytest.mark.django_db
def test_cash_commission_settles_pending_tds_payable(system_config):
    root = _make_verified_user("+918112000001")
    BinaryTreeService.place_member(root, None)
    sponsor = _make_verified_user("+918112000002")
    sponsor.sponsor = root
    sponsor.save()
    BinaryTreeService.place_member(sponsor, root)
    buyer = _make_verified_user("+918112000003")
    buyer.sponsor = sponsor
    buyer.save()
    BinaryTreeService.place_member(buyer, sponsor)

    Wallet.objects.update_or_create(
        user=sponsor,
        defaults={
            "total_earned": Decimal("0"),
            "current_band": 1,
            "cash_balance": Decimal("0"),
            "tds_payable": Decimal("20"),
        },
    )
    order = _paid_order(buyer, "ORD-SETTLE-CASH")
    CommissionEngine.process_order(order)

    w = Wallet.objects.get(user=sponsor)
    # 30 credited, 20 settled toward 194R payable -> cash = 10.
    assert w.cash_balance == Decimal("10.00")
    assert w.tds_payable == Decimal("0.00")
    assert WalletTransaction.objects.filter(
        user=sponsor,
        tx_type=WalletTransaction.TxType.TDS,
        reference__startswith="TDS-194R-SETTLE",
    ).exists()


@pytest.mark.django_db
def test_withdrawal_settles_pending_tds_payable_first(system_config):
    from apps.admin_panel.models import SystemConfig

    SystemConfig.objects.filter(pk=1).update(cooling_off_days=0)
    u = _make_verified_user("+918113000001")
    MemberComplianceProfile.objects.get_or_create(user=u)
    u.bank_account_number = "123456789012"
    u.bank_ifsc = "HDFC0001234"
    u.upi_id = "x@y"
    u.save()
    Wallet.objects.update_or_create(
        user=u,
        defaults={
            "cash_balance": Decimal("500"),
            "tds_payable": Decimal("50"),
            "current_band": 1,
        },
    )
    client = APIClient()
    client.force_authenticate(user=u)
    r = client.post(
        "/api/v1/user/wallet/withdraw/",
        {"amount": "200", "band": 1, "payout_method": "BANK"},
        format="json",
    )
    assert r.status_code == 200, r.content
    w = Wallet.objects.get(user=u)
    # 500 - 50 (settle) - 200 (withdraw) = 250
    assert w.cash_balance == Decimal("250.00")
    assert w.tds_payable == Decimal("0.00")


@pytest.mark.django_db
def test_slot_band_commission_reversal_unwinds_tds_payable(system_config):
    sponsor, buyer = _make_sponsor_tree_in_slot_band(Decimal("4000"))
    TdsLedger.objects.create(
        user=sponsor,
        financial_year=get_current_financial_year(),
        section=TdsLedger.SECTION_194R,
        total_earned=TDS_THRESHOLD_194R - Decimal("10"),
        total_tds=Decimal("0"),
        tds_triggered=False,
    )
    order = _paid_order(buyer, "ORD-194R-REV")
    CommissionEngine.process_order(order)
    entry = CommissionLedger.objects.get(recipient=sponsor, order=order)
    accrued = entry.tds_deducted
    assert accrued > Decimal("0")

    CommissionEngine.reverse_commissions(order)
    w = Wallet.objects.get(user=sponsor)
    assert w.tds_payable == Decimal("0.00")
    ledger = TdsLedger.objects.get(
        user=sponsor,
        financial_year=get_current_financial_year(),
        section=TdsLedger.SECTION_194R,
    )
    # total_earned reverts by entry.amount (30)
    assert ledger.total_earned == TDS_THRESHOLD_194R - Decimal("10")
    assert ledger.total_tds == Decimal("0.00")


@pytest.mark.django_db
def test_recompute_normalizes_194r_ledger_and_tds_payable(system_config):
    sponsor, buyer = _make_sponsor_tree_in_slot_band(Decimal("4000"))
    order = _paid_order(buyer, "ORD-194R-RECOMPUTE")
    CommissionEngine.process_order(order)

    # Pollute the 194R ledger to simulate the historical bug: 30 over-withheld
    # accrued on a 30 credit due to a bad cumulative total.
    fy = get_current_financial_year()
    ledger = TdsLedger.objects.get(
        user=sponsor, financial_year=fy, section=TdsLedger.SECTION_194R
    )
    ledger.total_earned = Decimal("30")
    ledger.total_tds = Decimal("30")
    ledger.tds_triggered = True
    ledger.save()
    Wallet.objects.filter(user=sponsor).update(tds_payable=Decimal("30"))
    CommissionLedger.objects.filter(pk=order.commission_entries.first().pk).update(
        tds_deducted=Decimal("30"),
        net_amount=Decimal("30"),  # slot-band cash stays equal to gross
    )

    call_command("recompute_tds_and_recredit", f"--fy={fy}", "--apply")

    w = Wallet.objects.get(user=sponsor)
    assert w.tds_payable == Decimal("0.00")
    fixed = TdsLedger.objects.get(
        user=sponsor, financial_year=fy, section=TdsLedger.SECTION_194R
    )
    assert fixed.total_earned == Decimal("30.00")
    assert fixed.total_tds == Decimal("0.00")
    fixed_entry = CommissionLedger.objects.get(recipient=sponsor, order=order)
    assert fixed_entry.tds_deducted == Decimal("0.00")
    assert fixed_entry.net_amount == Decimal("30.00")
