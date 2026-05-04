from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.admin_panel.models import SystemConfig
from apps.commissions.engine import CommissionEngine
from apps.commissions.models import CommissionLedger
from apps.mlm_tree import placement as placement_mod
from apps.mlm_tree.models import BinaryNode
from apps.mlm_tree.services import BinaryTreeService
from apps.mlm_tree.tasks import auto_place_pending_placements
from apps.payments.models import Order
from apps.users.models import User
from apps.users.services import allocate_member_identity


def _admin():
    return User.objects.create_user(
        login_identifier="place-admin@test.dev",
        password="pw",
        email="place-admin@test.dev",
        full_name="Place Admin",
        member_id="ADMPLACE01",
        referral_code="ADMPLC01",
        referral_link="http://localhost/join?ref=ADMPLC01",
        role=User.Role.SUPER_ADMIN,
        is_staff=True,
    )


def _member(phone: str, sponsor: User | None = None) -> User:
    mid, ref, link = allocate_member_identity()
    u = User(
        phone=phone,
        full_name="M",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        sponsor=sponsor,
    )
    u.set_unusable_password()
    u.save()
    return u


def _paid_order(user: User, **kwargs) -> Order:
    suffix = kwargs.pop("suffix", "x")
    defaults = dict(
        user=user,
        order_number=f"ORD-P-{user.id}-{suffix}",
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("241.72"),
        is_retail_purchase=False,
        status=Order.Status.PAID,
    )
    defaults.update(kwargs)
    return Order.objects.create(**defaults)


@pytest.mark.django_db
def test_find_slot_prefer_left_spillover(system_config):
    s = _member("+918010010010")
    s.is_member = True
    s.save(update_fields=["is_member"])
    BinaryTreeService.place_member(s, None)
    sn = s.binary_node
    a = _member("+918010010011", sponsor=s)
    a.is_member = True
    a.save(update_fields=["is_member"])
    BinaryTreeService.place_member_manual_leg(a, s, BinaryNode.Position.LEFT)
    b = _member("+918010010012", sponsor=s)
    b.is_member = True
    b.save(update_fields=["is_member"])
    BinaryTreeService.place_member_manual_leg(b, s, BinaryNode.Position.RIGHT)
    c = _member("+918010010013", sponsor=s)
    sn.refresh_from_db()
    parent, pos = BinaryTreeService._find_slot_prefer_leg(sn, BinaryNode.Position.LEFT)
    assert parent.user_id == a.id
    assert pos == BinaryNode.Position.LEFT


@pytest.mark.django_db
def test_open_placement_queue_and_manual_place_commissions(system_config):
    sponsor = _member("+918020020020")
    sponsor.is_member = True
    sponsor.save(update_fields=["is_member"])
    BinaryTreeService.place_member(sponsor, None)
    buyer = _member("+918020020021", sponsor=sponsor)
    order = _paid_order(buyer, suffix="a")
    placement_mod.open_placement_queue_if_needed(order, buyer)
    order.refresh_from_db()
    assert order.placement_status == Order.PlacementStatus.PENDING
    assert order.placement_deadline_at

    client = APIClient()
    client.force_authenticate(user=sponsor)
    r = client.post(
        "/api/v1/user/tree/place-direct/",
        {"member_id": buyer.member_id, "leg": "LEFT"},
        format="json",
    )
    assert r.status_code == 200, r.content
    buyer.refresh_from_db()
    assert hasattr(buyer, "binary_node")
    assert CommissionLedger.objects.filter(order=order).exists()
    CommissionEngine.process_order(order)
    n0 = CommissionLedger.objects.filter(order=order).count()
    CommissionEngine.process_order(order)
    assert CommissionLedger.objects.filter(order=order).count() == n0


@pytest.mark.django_db
def test_auto_place_after_deadline(system_config):
    SystemConfig.objects.filter(pk=1).update(auto_placement_strategy=SystemConfig.AutoPlacementStrategy.LEFT_FIRST)
    sponsor = _member("+918030030030")
    sponsor.is_member = True
    sponsor.save(update_fields=["is_member"])
    BinaryTreeService.place_member(sponsor, None)
    buyer = _member("+918030030031", sponsor=sponsor)
    order = _paid_order(buyer, suffix="b")
    order.placement_status = Order.PlacementStatus.PENDING
    order.placement_deadline_at = timezone.now() - timedelta(minutes=1)
    order.save(update_fields=["placement_status", "placement_deadline_at"])
    auto_place_pending_placements()
    buyer.refresh_from_db()
    assert hasattr(buyer, "binary_node")
    order.refresh_from_db()
    assert order.placement_status == Order.PlacementStatus.PLACED_AUTO


@pytest.mark.django_db
def test_admin_reverse_leaf_and_block_non_leaf(system_config):
    admin = _admin()
    sponsor = _member("+918040040040")
    sponsor.is_member = True
    sponsor.save(update_fields=["is_member"])
    BinaryTreeService.place_member(sponsor, None)
    buyer = _member("+918040040041", sponsor=sponsor)
    order = _paid_order(buyer, suffix="c")
    placement_mod.complete_placement_for_order(
        order,
        manual_leg=BinaryNode.Position.LEFT,
        auto_strategy=None,
        final_status=Order.PlacementStatus.PLACED_MANUAL,
        actor=sponsor,
        audit_action="placement.manual",
    )
    assert CommissionLedger.objects.filter(order=order).exists()

    client = APIClient()
    client.force_authenticate(user=admin)
    r = client.post(f"/api/v1/admin/placements/{order.id}/reverse/")
    assert r.status_code == 200, r.content
    buyer.refresh_from_db()
    assert not hasattr(buyer, "binary_node")
    order.refresh_from_db()
    assert order.placement_status == Order.PlacementStatus.PENDING

    placement_mod.complete_placement_for_order(
        order,
        manual_leg=BinaryNode.Position.LEFT,
        auto_strategy=None,
        final_status=Order.PlacementStatus.PLACED_MANUAL,
        actor=sponsor,
        audit_action="placement.manual",
    )
    u2 = _member("+918040040042", sponsor=buyer)
    u2.is_member = True
    u2.save(update_fields=["is_member"])
    BinaryTreeService.place_member(u2, buyer)
    buyer.refresh_from_db()
    assert buyer.binary_node.left_child_id or buyer.binary_node.right_child_id

    r2 = client.post(f"/api/v1/admin/placements/{order.id}/reverse/")
    assert r2.status_code == 409
