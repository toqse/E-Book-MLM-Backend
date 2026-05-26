"""reconcile_slot_overshoot management command."""

from decimal import Decimal

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.commissions.management.commands.reconcile_slot_overshoot import (
    _compute_overshoot,
    reconcile_wallet,
)
from apps.commissions.models import CommissionLedger
from apps.payments.models import Order
from apps.users.models import User
from apps.users.services import allocate_member_identity
from apps.wallet.models import Wallet, WalletTransaction


def _minimal_order(user: User, order_number: str) -> Order:
    now = timezone.now()
    return Order.objects.create(
        user=user,
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


def _user(phone: str = "+918220000001") -> User:
    mid, ref, link = allocate_member_identity()
    u = User(
        phone=phone,
        full_name="Reconcile Test",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        kyc_status=User.KYCStatus.VERIFIED,
    )
    u.set_unusable_password()
    u.save()
    return u


@pytest.mark.django_db
def test_replay_detects_overshoot_on_legacy_single_row(system_config):
    """One ₹500 slot-tagged row at 14800 should have been 200 slot + 300 cash."""
    u = _user()
    w = Wallet.objects.create(
        user=u,
        total_earned=Decimal("15300"),
        cash_balance=Decimal("0"),
        total_withdrawn=Decimal("0"),
        current_band=7,
    )
    now = timezone.now()
    o1 = _minimal_order(u, "ORD-RECON-PRIOR")
    o2 = _minimal_order(u, "ORD-RECON-OVER")
    CommissionLedger.objects.create(
        recipient=u,
        source_user=u,
        order=o1,
        commission_type=CommissionLedger.CommissionType.DIRECT,
        amount=Decimal("14800"),
        tds_deducted=Decimal("0"),
        net_amount=Decimal("14800"),
        status=CommissionLedger.Status.CREDITED,
        slot_band_held=False,
        created_at=now - timezone.timedelta(hours=1),
    )
    CommissionLedger.objects.create(
        recipient=u,
        source_user=u,
        order=o2,
        commission_type=CommissionLedger.CommissionType.DIRECT,
        amount=Decimal("500"),
        tds_deducted=Decimal("0"),
        net_amount=Decimal("500"),
        status=CommissionLedger.Status.CREDITED,
        slot_band_held=True,
        created_at=now,
    )

    slot_current, slot_correct, overshoot = _compute_overshoot(user_id=u.pk)
    assert slot_current == Decimal("500")
    assert overshoot == Decimal("300")


@pytest.mark.django_db
def test_conversation_user_shape_reconcile(system_config):
    """Wallet at 14800 + legacy ₹520 slot row → ₹320 overshoot → cash 7810→8130."""
    u = _user("+918220000099")
    w = Wallet.objects.create(
        user=u,
        total_earned=Decimal("15320"),
        cash_balance=Decimal("7810"),
        total_withdrawn=Decimal("3670"),
        total_tds_deducted=Decimal("0"),
        tds_payable=Decimal("0"),
        current_band=6,
    )
    now = timezone.now()
    o1 = _minimal_order(u, "ORD-CONV-PRIOR")
    o2 = _minimal_order(u, "ORD-CONV-OVER")
    CommissionLedger.objects.create(
        recipient=u,
        source_user=u,
        order=o1,
        commission_type=CommissionLedger.CommissionType.DIRECT,
        amount=Decimal("14800"),
        tds_deducted=Decimal("0"),
        net_amount=Decimal("14800"),
        status=CommissionLedger.Status.CREDITED,
        slot_band_held=False,
        created_at=now - timezone.timedelta(days=2),
    )
    CommissionLedger.objects.create(
        recipient=u,
        source_user=u,
        order=o2,
        commission_type=CommissionLedger.CommissionType.DIRECT,
        amount=Decimal("520"),
        tds_deducted=Decimal("0"),
        net_amount=Decimal("520"),
        status=CommissionLedger.Status.CREDITED,
        slot_band_held=True,
        created_at=now - timezone.timedelta(days=1),
    )

    slot_current, slot_correct, overshoot = _compute_overshoot(user_id=u.pk)
    assert slot_current == Decimal("520")
    assert overshoot == Decimal("320")

    result = reconcile_wallet(w, apply=True, dry_run=False)
    assert result.action == "applied"
    assert result.overshoot == Decimal("320")

    w.refresh_from_db()
    assert w.cash_balance == Decimal("8130")
    assert w.total_earned == Decimal("15320")
    assert WalletTransaction.objects.filter(
        user=u,
        reference=f"RECONCILE-SLOT-OVERSHOOT:{w.pk}",
        tx_type=WalletTransaction.TxType.ADJUSTMENT,
    ).count() == 1


@pytest.mark.django_db
def test_reconcile_idempotent(system_config):
    u = _user("+918220000002")
    w = Wallet.objects.create(
        user=u,
        total_earned=Decimal("15300"),
        cash_balance=Decimal("0"),
        total_withdrawn=Decimal("0"),
    )
    now = timezone.now()
    o1 = _minimal_order(u, "ORD-IDEM-1")
    o2 = _minimal_order(u, "ORD-IDEM-2")
    CommissionLedger.objects.create(
        recipient=u,
        source_user=u,
        order=o1,
        commission_type=CommissionLedger.CommissionType.DIRECT,
        amount=Decimal("14800"),
        tds_deducted=Decimal("0"),
        net_amount=Decimal("14800"),
        status=CommissionLedger.Status.CREDITED,
        slot_band_held=False,
        created_at=now - timezone.timedelta(hours=1),
    )
    CommissionLedger.objects.create(
        recipient=u,
        source_user=u,
        order=o2,
        commission_type=CommissionLedger.CommissionType.DIRECT,
        amount=Decimal("500"),
        tds_deducted=Decimal("0"),
        net_amount=Decimal("500"),
        status=CommissionLedger.Status.CREDITED,
        slot_band_held=True,
        created_at=now,
    )

    call_command("reconcile_slot_overshoot", "--apply", user_id=u.pk)
    w.refresh_from_db()
    bal_after_first = w.cash_balance
    call_command("reconcile_slot_overshoot", "--apply", user_id=u.pk)
    w.refresh_from_db()
    assert w.cash_balance == bal_after_first


@pytest.mark.django_db
def test_reconcile_skips_when_tds_on_slot_rows(system_config):
    u = _user("+918220000003")
    w = Wallet.objects.create(
        user=u,
        total_earned=Decimal("15300"),
        cash_balance=Decimal("0"),
        total_tds_deducted=Decimal("0"),
        tds_payable=Decimal("10"),
    )
    now = timezone.now()
    o1 = _minimal_order(u, "ORD-TDS-1")
    o2 = _minimal_order(u, "ORD-TDS-2")
    CommissionLedger.objects.create(
        recipient=u,
        source_user=u,
        order=o1,
        commission_type=CommissionLedger.CommissionType.DIRECT,
        amount=Decimal("14800"),
        tds_deducted=Decimal("0"),
        net_amount=Decimal("14800"),
        status=CommissionLedger.Status.CREDITED,
        slot_band_held=False,
        created_at=now - timezone.timedelta(hours=1),
    )
    CommissionLedger.objects.create(
        recipient=u,
        source_user=u,
        order=o2,
        commission_type=CommissionLedger.CommissionType.DIRECT,
        amount=Decimal("500"),
        tds_deducted=Decimal("5"),
        net_amount=Decimal("500"),
        status=CommissionLedger.Status.CREDITED,
        slot_band_held=True,
        created_at=now,
    )

    result = reconcile_wallet(w, apply=True, dry_run=False)
    assert result.action == "skip_tds"
    w.refresh_from_db()
    assert w.cash_balance == Decimal("0")


@pytest.mark.django_db
def test_dry_run_does_not_mutate(system_config):
    u = _user("+918220000004")
    w = Wallet.objects.create(
        user=u,
        total_earned=Decimal("15300"),
        cash_balance=Decimal("0"),
    )
    now = timezone.now()
    o1 = _minimal_order(u, "ORD-DRY-1")
    o2 = _minimal_order(u, "ORD-DRY-2")
    CommissionLedger.objects.create(
        recipient=u,
        source_user=u,
        order=o1,
        commission_type=CommissionLedger.CommissionType.DIRECT,
        amount=Decimal("14800"),
        tds_deducted=Decimal("0"),
        net_amount=Decimal("14800"),
        status=CommissionLedger.Status.CREDITED,
        slot_band_held=False,
        created_at=now - timezone.timedelta(hours=1),
    )
    CommissionLedger.objects.create(
        recipient=u,
        source_user=u,
        order=o2,
        commission_type=CommissionLedger.CommissionType.DIRECT,
        amount=Decimal("500"),
        tds_deducted=Decimal("0"),
        net_amount=Decimal("500"),
        status=CommissionLedger.Status.CREDITED,
        slot_band_held=True,
        created_at=now,
    )

    call_command("reconcile_slot_overshoot", "--dry-run", user_id=u.pk)
    w.refresh_from_db()
    assert w.cash_balance == Decimal("0")
    assert not WalletTransaction.objects.filter(
        reference=f"RECONCILE-SLOT-OVERSHOOT:{w.pk}"
    ).exists()
