from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.mlm_tree import placement as placement_mod
from apps.mlm_tree.models import BinaryNode
from apps.mlm_tree.services import BinaryTreeService
from apps.payments.models import Order
from apps.users.models import User
from apps.users.services import allocate_member_identity


def _admin():
    return User.objects.create_user(
        login_identifier="bt-admin@test.dev",
        password="pw",
        email="bt-admin@test.dev",
        full_name="BT Admin",
        member_id="BTADMIN01",
        referral_code="BTAD01",
        referral_link="http://localhost/join?ref=BTAD01",
        role=User.Role.SUPER_ADMIN,
        is_staff=True,
    )


def _member(phone: str, sponsor: User | None = None, *, name: str = "M") -> User:
    mid, ref, link = allocate_member_identity()
    u = User(
        phone=phone,
        full_name=name,
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        sponsor=sponsor,
        is_member=True,
    )
    u.set_unusable_password()
    u.save()
    return u


def _paid_order(user: User, **kwargs) -> Order:
    suffix = kwargs.pop("suffix", "x")
    defaults = dict(
        user=user,
        order_number=f"ORD-BT-{user.id}-{suffix}",
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("241.72"),
        is_retail_purchase=False,
        status=Order.Status.PAID,
        paid_at=timezone.now(),
        refund_eligible_until=timezone.now() + timedelta(days=7),
    )
    defaults.update(kwargs)
    return Order.objects.create(**defaults)


@pytest.mark.django_db
def test_admin_place_under_parent_within_cooloff_updates_cached_sizes(system_config):
    admin = _admin()
    client = APIClient()
    client.force_authenticate(user=admin)

    root = _member("+919000000001", sponsor=None, name="Root")
    BinaryTreeService.place_member(root, None)

    parent2 = _member("+919000000002", sponsor=root, name="Parent2")
    # Place Parent2 under root RIGHT so it becomes the anchor for the move.
    o2 = _paid_order(parent2, suffix="p2")
    placement_mod.complete_placement_for_order(
        o2,
        manual_leg=BinaryNode.Position.RIGHT,
        auto_strategy=None,
        final_status=Order.PlacementStatus.PLACED_MANUAL,
        actor=admin,
        audit_action="placement.test",
    )

    buyer = _member("+919000000003", sponsor=root, name="Buyer")
    order = _paid_order(buyer, suffix="b1")
    placement_mod.complete_placement_for_order(
        order,
        manual_leg=BinaryNode.Position.LEFT,
        auto_strategy=None,
        final_status=Order.PlacementStatus.PLACED_MANUAL,
        actor=admin,
        audit_action="placement.test",
    )

    root_node = BinaryNode.objects.get(user=root)
    assert root_node.left_subtree_size == 1
    assert root_node.right_subtree_size == 1

    # Move buyer under Parent2 RIGHT (within cool-off).
    r = client.post(
        f"/api/v1/admin/binary-tree/placements/{order.id}/place/",
        {"parent_member_id": parent2.member_id, "leg": "RIGHT"},
        format="json",
    )
    assert r.status_code == 200, r.content

    buyer.refresh_from_db()
    assert hasattr(buyer, "binary_node")
    buyer_node = buyer.binary_node
    assert buyer_node.parent.user_id == parent2.id

    root_node.refresh_from_db()
    # Buyer moved into root's RIGHT subtree (under Parent2).
    assert root_node.left_subtree_size == 0
    assert root_node.right_subtree_size == 2


@pytest.mark.django_db
def test_admin_place_under_parent_denied_after_cooloff(system_config):
    admin = _admin()
    client = APIClient()
    client.force_authenticate(user=admin)

    root = _member("+919000000011", sponsor=None, name="Root")
    BinaryTreeService.place_member(root, None)

    buyer = _member("+919000000012", sponsor=root, name="Buyer")
    order = _paid_order(
        buyer,
        suffix="b2",
        refund_eligible_until=timezone.now() - timedelta(seconds=1),
    )
    placement_mod.complete_placement_for_order(
        order,
        manual_leg=BinaryNode.Position.LEFT,
        auto_strategy=None,
        final_status=Order.PlacementStatus.PLACED_MANUAL,
        actor=admin,
        audit_action="placement.test",
    )

    r = client.post(
        f"/api/v1/admin/binary-tree/placements/{order.id}/place/",
        {"parent_member_id": root.member_id, "leg": "LEFT"},
        format="json",
    )
    assert r.status_code == 400


@pytest.mark.django_db
def test_admin_pending_binary_placements_excludes_super_admin(system_config):
    admin = _admin()
    client = APIClient()
    client.force_authenticate(user=admin)

    super_admin_order = _paid_order(
        admin,
        suffix="admin",
        placement_status=Order.PlacementStatus.PENDING,
        placement_deadline_at=timezone.now() + timedelta(hours=24),
    )

    sponsor = _member("+919000000021", name="Sponsor")
    member = _member("+919000000022", sponsor=sponsor, name="Member")
    member_order = _paid_order(
        member,
        suffix="member",
        placement_status=Order.PlacementStatus.PENDING,
        placement_deadline_at=timezone.now() + timedelta(hours=24),
    )

    r = client.get("/api/v1/admin/binary-tree/placements/pending/")

    assert r.status_code == 200, r.content
    body = r.json()["data"]
    order_ids = {row["order_id"] for row in body["results"]}
    assert member_order.id in order_ids
    assert super_admin_order.id not in order_ids
    assert body["count"] == 1

