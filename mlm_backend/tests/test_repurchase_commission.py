from decimal import Decimal

import pytest
from django.utils import timezone

from apps.admin_panel.models import SystemConfig
from apps.commissions.engine import CommissionEngine
from apps.commissions.models import CommissionLedger
from apps.mlm_tree.services import BinaryTreeService
from apps.payments.models import Order
from apps.users.models import User
from apps.users.services import allocate_member_identity


def _paid_mlm_order(user: User, suffix: str) -> Order:
    return Order.objects.create(
        user=user,
        order_number=f"ORD-RP-{suffix}",
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("241.72"),
        is_retail_purchase=False,
        status=Order.Status.PAID,
        paid_at=timezone.now(),
        refund_eligible_until=timezone.now() - timezone.timedelta(days=1),
    )


@pytest.mark.django_db
def test_repurchase_skipped_when_flag_false(system_config):
    cfg = SystemConfig.objects.get(pk=1)
    cfg.is_repurchase_commission_allowed = False
    cfg.save(update_fields=["is_repurchase_commission_allowed"])

    mid_r, r_r, l_r = allocate_member_identity()
    root = User(
        phone="+917700000001",
        full_name="Root",
        member_id=mid_r,
        referral_code=r_r,
        referral_link=l_r,
    )
    root.set_unusable_password()
    root.save()
    root.is_member = True
    root.save()
    BinaryTreeService.place_member(root, None)

    mid_s, r_s, l_s = allocate_member_identity()
    sponsor = User(
        phone="+917700000002",
        full_name="S",
        member_id=mid_s,
        referral_code=r_s,
        referral_link=l_s,
        sponsor=root,
    )
    sponsor.set_unusable_password()
    sponsor.save()
    sponsor.is_member = True
    sponsor.save()
    BinaryTreeService.place_member(sponsor, root)

    mid_b, r_b, l_b = allocate_member_identity()
    buyer = User(
        phone="+917700000003",
        full_name="B",
        member_id=mid_b,
        referral_code=r_b,
        referral_link=l_b,
        sponsor=sponsor,
    )
    buyer.set_unusable_password()
    buyer.save()
    buyer.is_member = True
    buyer.save()
    BinaryTreeService.place_member(buyer, sponsor)

    o1 = _paid_mlm_order(buyer, "1")
    CommissionEngine.process_order(o1)
    n1 = CommissionLedger.objects.filter(order=o1).count()
    assert n1 >= 1

    o2 = _paid_mlm_order(buyer, "2")
    CommissionEngine.process_order(o2)
    assert CommissionLedger.objects.filter(order=o2).count() == 0


@pytest.mark.django_db
def test_repurchase_allowed_when_flag_true(system_config):
    cfg = SystemConfig.objects.get(pk=1)
    cfg.is_repurchase_commission_allowed = True
    cfg.save(update_fields=["is_repurchase_commission_allowed"])

    mid_r, r_r, l_r = allocate_member_identity()
    root = User(
        phone="+917700000011",
        full_name="Root2",
        member_id=mid_r,
        referral_code=r_r,
        referral_link=l_r,
    )
    root.set_unusable_password()
    root.save()
    root.is_member = True
    root.save()
    BinaryTreeService.place_member(root, None)

    mid_s, r_s, l_s = allocate_member_identity()
    sponsor = User(
        phone="+917700000012",
        full_name="S2",
        member_id=mid_s,
        referral_code=r_s,
        referral_link=l_s,
        sponsor=root,
    )
    sponsor.set_unusable_password()
    sponsor.save()
    sponsor.is_member = True
    sponsor.save()
    BinaryTreeService.place_member(sponsor, root)

    mid_b, r_b, l_b = allocate_member_identity()
    buyer = User(
        phone="+917700000013",
        full_name="B2",
        member_id=mid_b,
        referral_code=r_b,
        referral_link=l_b,
        sponsor=sponsor,
    )
    buyer.set_unusable_password()
    buyer.save()
    buyer.is_member = True
    buyer.save()
    BinaryTreeService.place_member(buyer, sponsor)

    o1 = _paid_mlm_order(buyer, "a")
    CommissionEngine.process_order(o1)
    o2 = _paid_mlm_order(buyer, "b")
    CommissionEngine.process_order(o2)
    assert CommissionLedger.objects.filter(order=o2).exists()
