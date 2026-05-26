"""Band-split commission/milestone crediting at band boundaries."""

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.admin_panel.utils import get_system_config
from apps.commissions.engine import CommissionEngine
from apps.commissions.models import CommissionLedger, MilestoneRecord
from apps.mlm_tree.services import BinaryTreeService
from apps.payments.models import Order
from apps.users.models import User
from apps.users.services import allocate_member_identity
from apps.wallet.bands import iter_band_split_pieces, slot_gross_if_split_at
from apps.wallet.models import Wallet


def _make_verified_user(phone: str) -> User:
    mid, ref, link = allocate_member_identity()
    u = User(
        phone=phone,
        full_name="Test",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        pan_number="ABCDE1234F",
        kyc_status=User.KYCStatus.VERIFIED,
    )
    u.set_unusable_password()
    u.save()
    return u


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


def _tree_at_total_earned(total: Decimal, *, current_band: int) -> tuple[User, User]:
    root = _make_verified_user("+918210000001")
    BinaryTreeService.place_member(root, None)
    sponsor = _make_verified_user("+918210000002")
    sponsor.sponsor = root
    sponsor.save()
    BinaryTreeService.place_member(sponsor, root)
    Wallet.objects.update_or_create(
        user=sponsor,
        defaults={
            "total_earned": total,
            "current_band": current_band,
            "cash_balance": Decimal("0"),
        },
    )
    buyer = _make_verified_user("+918210000003")
    buyer.sponsor = sponsor
    buyer.save()
    BinaryTreeService.place_member(buyer, sponsor)
    return sponsor, buyer


@pytest.mark.django_db
def test_iter_band_split_500_at_band6_edge():
    pieces = list(
        iter_band_split_pieces(
            total_earned=Decimal("14800"),
            gross=Decimal("500"),
            cap=Decimal("22200"),
        )
    )
    assert pieces == [(Decimal("200"), True), (Decimal("300"), False)]


@pytest.mark.django_db
def test_iter_band_split_6000_crosses_four_bands():
    pieces = list(
        iter_band_split_pieces(
            total_earned=Decimal("14800"),
            gross=Decimal("6000"),
            cap=Decimal("22200"),
        )
    )
    assert pieces == [
        (Decimal("200"), True),
        (Decimal("4000"), False),
        (Decimal("1000"), True),
        (Decimal("800"), False),
    ]


@pytest.mark.django_db
def test_slot_gross_if_split_at_matches_sum_of_slot_pieces():
    assert slot_gross_if_split_at(
        total_before=Decimal("14800"), amount=Decimal("500")
    ) == Decimal("200")


@pytest.mark.django_db
def test_commission_splits_at_band6_boundary(system_config):
    sponsor, buyer = _tree_at_total_earned(
        Decimal("14800"), current_band=6
    )
    order = _paid_order(buyer, "ORD-SPLIT-500")
    # Simulate a large credit by directly calling _credit_user with gross=500
    CommissionEngine._credit_user(
        recipient=sponsor,
        source=buyer,
        order=order,
        ctype=CommissionLedger.CommissionType.DIRECT,
        gross=Decimal("500"),
        cap=get_system_config().earning_cap,
    )
    entries = list(
        CommissionLedger.objects.filter(recipient=sponsor, order=order).order_by(
            "id"
        )
    )
    assert len(entries) == 2
    assert entries[0].amount == Decimal("200")
    assert entries[0].slot_band_held is True
    assert entries[1].amount == Decimal("300")
    assert entries[1].slot_band_held is False

    w = Wallet.objects.get(user=sponsor)
    assert w.total_earned == Decimal("15300")
    assert w.cash_balance == Decimal("300")


@pytest.mark.django_db
def test_single_30_commission_wholly_in_slot_band(system_config):
    sponsor, buyer = _tree_at_total_earned(Decimal("4000"), current_band=2)
    order = _paid_order(buyer, "ORD-SLOT-30")
    CommissionEngine.process_order(order)
    entries = CommissionLedger.objects.filter(recipient=sponsor, order=order)
    assert entries.count() == 1
    assert entries.get().amount == Decimal("30")
    assert entries.get().slot_band_held is True
    w = Wallet.objects.get(user=sponsor)
    assert w.cash_balance == Decimal("0")
    assert w.total_earned == Decimal("4030")


@pytest.mark.django_db
def test_single_30_commission_wholly_in_cash_band(system_config):
    sponsor, buyer = _tree_at_total_earned(Decimal("0"), current_band=1)
    order = _paid_order(buyer, "ORD-CASH-30")
    CommissionEngine.process_order(order)
    entries = CommissionLedger.objects.filter(recipient=sponsor, order=order)
    assert entries.count() == 1
    assert entries.get().slot_band_held is False
    w = Wallet.objects.get(user=sponsor)
    assert w.cash_balance == Decimal("30")
    assert w.total_earned == Decimal("30")


@pytest.mark.django_db
def test_milestone_splits_at_band_boundary(system_config):
    sponsor, buyer = _tree_at_total_earned(
        Decimal("14800"), current_band=6
    )
    sponsor.direct_referral_count = 10
    sponsor.save()
    CommissionEngine._maybe_milestone(sponsor, get_system_config())
    rows = list(
        MilestoneRecord.objects.filter(
            user=sponsor, milestone_referrals=10, status="CREDITED"
        ).order_by("id")
    )
    assert len(rows) == 2
    assert rows[0].bonus_amount == Decimal("200")
    assert rows[0].slot_band_held is True
    assert rows[1].bonus_amount == Decimal("100")
    assert rows[1].slot_band_held is False
    w = Wallet.objects.get(user=sponsor)
    assert w.total_earned == Decimal("15100")
    assert w.cash_balance == Decimal("100")


@pytest.mark.django_db
def test_reversal_unwinds_split_commission_rows(system_config):
    sponsor, buyer = _tree_at_total_earned(
        Decimal("14800"), current_band=6
    )
    order = _paid_order(buyer, "ORD-SPLIT-REV")
    CommissionEngine._credit_user(
        recipient=sponsor,
        source=buyer,
        order=order,
        ctype=CommissionLedger.CommissionType.DIRECT,
        gross=Decimal("500"),
        cap=get_system_config().earning_cap,
    )
    w_before = Wallet.objects.get(user=sponsor)
    cash_before = w_before.cash_balance
    earned_before = w_before.total_earned

    CommissionEngine.reverse_commissions(order)
    w = Wallet.objects.get(user=sponsor)
    assert w.cash_balance == cash_before - Decimal("300")
    assert w.total_earned == earned_before - Decimal("500")
    assert (
        CommissionLedger.objects.filter(
            recipient=sponsor, order=order, status=CommissionLedger.Status.REVERSED
        ).count()
        == 2
    )
