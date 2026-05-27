from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.admin_panel.models import SystemConfig
from apps.agreements.models import MemberComplianceProfile
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
    now = timezone.now()
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
        paid_at=now,
        refund_eligible_until=now + timedelta(days=7),
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
def test_open_placement_queue_and_manual_place_commissions(
    system_config, django_capture_on_commit_callbacks
):
    sponsor = _member("+918020020020")
    sponsor.is_member = True
    sponsor.save(update_fields=["is_member"])
    sponsor.kyc_status = User.KYCStatus.VERIFIED
    sponsor.save(update_fields=["kyc_status"])
    MemberComplianceProfile.objects.create(user=sponsor)
    BinaryTreeService.place_member(sponsor, None)
    buyer = _member("+918020020021", sponsor=sponsor)
    order = _paid_order(buyer, suffix="a")
    placement_mod.open_placement_queue_if_needed(order, buyer)
    order.refresh_from_db()
    assert order.placement_status == Order.PlacementStatus.PENDING
    assert order.placement_deadline_at

    client = APIClient()
    client.force_authenticate(user=sponsor)
    with django_capture_on_commit_callbacks(execute=True):
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
def test_auto_place_after_deadline(system_config, django_capture_on_commit_callbacks):
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
    with django_capture_on_commit_callbacks(execute=True):
        auto_place_pending_placements()
    buyer.refresh_from_db()
    assert hasattr(buyer, "binary_node")
    order.refresh_from_db()
    assert order.placement_status == Order.PlacementStatus.PLACED_AUTO


@pytest.mark.django_db
def test_admin_reverse_leaf_and_block_non_leaf(
    system_config, django_capture_on_commit_callbacks
):
    admin = _admin()
    sponsor = _member("+918040040040")
    sponsor.is_member = True
    sponsor.save(update_fields=["is_member"])
    BinaryTreeService.place_member(sponsor, None)
    buyer = _member("+918040040041", sponsor=sponsor)
    order = _paid_order(buyer, suffix="c")
    with django_capture_on_commit_callbacks(execute=True):
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

    with django_capture_on_commit_callbacks(execute=True):
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


def _verified_with_compliance(user: User) -> None:
    user.kyc_status = User.KYCStatus.VERIFIED
    user.kyc_first_approved_at = timezone.now()
    user.save(update_fields=["kyc_status", "kyc_first_approved_at"])
    MemberComplianceProfile.objects.get_or_create(user=user)


@pytest.mark.django_db
def test_user_place_blocked_when_sponsor_unplaced(system_config):
    """B must not be able to place C while B itself is not placed in the binary tree."""
    root = _member("+918050050050")
    BinaryTreeService.place_member(root, None)
    sponsor_a = _member("+918050050051", sponsor=root)
    _verified_with_compliance(sponsor_a)
    buyer_b = _member("+918050050052", sponsor=sponsor_a)
    _paid_order(buyer_b, suffix="bself")
    _verified_with_compliance(buyer_b)
    buyer_c = _member("+918050050053", sponsor=buyer_b)
    c_order = _paid_order(buyer_c, suffix="cself")
    c_order.placement_status = Order.PlacementStatus.PENDING
    c_order.placement_deadline_at = timezone.now() + timedelta(hours=24)
    c_order.save(update_fields=["placement_status", "placement_deadline_at"])

    client = APIClient()
    client.force_authenticate(user=buyer_b)
    r = client.post(
        "/api/v1/user/tree/place-direct/",
        {"member_id": buyer_c.member_id, "leg": "LEFT"},
        format="json",
    )
    assert r.status_code == 400, r.content
    body = r.json()
    assert "before you can place your referrals" in body.get("message", "")

    buyer_b.refresh_from_db()
    assert not hasattr(buyer_b, "binary_node"), "B must NOT be silently rooted into the tree"
    buyer_c.refresh_from_db()
    assert not hasattr(buyer_c, "binary_node"), "C must remain unplaced"


@pytest.mark.django_db
def test_user_place_succeeds_once_sponsor_is_placed(
    system_config, django_capture_on_commit_callbacks
):
    """After A places B, B can place C cleanly (regression: happy path)."""
    root = _member("+918060060060")
    BinaryTreeService.place_member(root, None)
    sponsor_a = _member("+918060060061", sponsor=root)
    _verified_with_compliance(sponsor_a)
    BinaryTreeService.place_member_manual_leg(sponsor_a, root, BinaryNode.Position.LEFT)

    buyer_b = _member("+918060060062", sponsor=sponsor_a)
    b_order = _paid_order(buyer_b, suffix="bok")
    placement_mod.open_placement_queue_if_needed(b_order, buyer_b)
    _verified_with_compliance(buyer_b)
    client = APIClient()
    client.force_authenticate(user=sponsor_a)
    with django_capture_on_commit_callbacks(execute=True):
        r = client.post(
            "/api/v1/user/tree/place-direct/",
            {"member_id": buyer_b.member_id, "leg": "LEFT"},
            format="json",
        )
    assert r.status_code == 200, r.content
    buyer_b.refresh_from_db()
    assert hasattr(buyer_b, "binary_node")

    buyer_c = _member("+918060060063", sponsor=buyer_b)
    c_order = _paid_order(buyer_c, suffix="cok")
    placement_mod.open_placement_queue_if_needed(c_order, buyer_c)

    client.force_authenticate(user=buyer_b)
    with django_capture_on_commit_callbacks(execute=True):
        r2 = client.post(
            "/api/v1/user/tree/place-direct/",
            {"member_id": buyer_c.member_id, "leg": "LEFT"},
            format="json",
        )
    assert r2.status_code == 200, r2.content
    buyer_c.refresh_from_db()
    assert hasattr(buyer_c, "binary_node")
    assert buyer_c.binary_node.parent.user_id == buyer_b.id


@pytest.mark.django_db
def test_primitive_refuses_silent_root_for_unplaced_sponsor(system_config):
    """Last-line-of-defense: the tree primitive itself must never auto-root the sponsor."""
    sponsor_b = _member("+918070070070")
    buyer_c = _member("+918070070071", sponsor=sponsor_b)

    with pytest.raises(ValueError) as exc:
        BinaryTreeService.place_member_manual_leg(buyer_c, sponsor_b, BinaryNode.Position.LEFT)
    assert "not placed in the binary tree" in str(exc.value).lower()

    with pytest.raises(ValueError) as exc2:
        BinaryTreeService.place_member_auto(buyer_c, sponsor_b, None)
    assert "not placed in the binary tree" in str(exc2.value).lower()

    sponsor_b.refresh_from_db()
    assert not BinaryNode.objects.filter(user=sponsor_b).exists()
    assert not BinaryNode.objects.filter(user=buyer_c).exists()


@pytest.mark.django_db
def test_admin_reassign_refuses_unplaced_sponsor(system_config):
    """Admin reassign must refuse to place a buyer whose sponsor is not in the tree."""
    admin = _admin()
    root = _member("+918080080080")
    BinaryTreeService.place_member(root, None)
    sponsor_a = _member("+918080080081", sponsor=root)
    buyer_b = _member("+918080080082", sponsor=sponsor_a)
    b_order = _paid_order(buyer_b, suffix="badm")

    client = APIClient()
    client.force_authenticate(user=admin)
    r = client.post(
        f"/api/v1/admin/placements/{b_order.id}/reassign/",
        {"leg": "LEFT"},
        format="json",
    )
    assert r.status_code == 400, r.content
    body = r.json()
    assert "sponsor is not placed" in body.get("message", "").lower()
    buyer_b.refresh_from_db()
    assert not hasattr(buyer_b, "binary_node")


@pytest.mark.django_db
def test_admin_place_under_parent_refuses_unplaced_parent(system_config):
    """Admin place-under-parent must refuse a parent_member_id that is not in the tree."""
    admin = _admin()
    root = _member("+918090090090")
    BinaryTreeService.place_member(root, None)
    unplaced_parent = _member("+918090090091", sponsor=root)
    buyer = _member("+918090090092", sponsor=unplaced_parent)
    order = _paid_order(buyer, suffix="bup")

    client = APIClient()
    client.force_authenticate(user=admin)
    r = client.post(
        f"/api/v1/admin/binary-tree/placements/{order.id}/place/",
        {"parent_member_id": unplaced_parent.member_id, "leg": "LEFT"},
        format="json",
    )
    assert r.status_code == 400, r.content
    body = r.json()
    assert "not placed in the binary tree" in body.get("message", "").lower()
    buyer.refresh_from_db()
    assert not hasattr(buyer, "binary_node")


@pytest.mark.django_db
def test_auto_placer_marks_failed_when_sponsor_unplaced(
    system_config, django_capture_on_commit_callbacks
):
    """When the auto-placer hits a buyer whose sponsor is unplaced, the order is marked FAILED with a clear reason - it is NOT silently rooted."""
    root = _member("+918100100100")
    BinaryTreeService.place_member(root, None)
    sponsor_a = _member("+918100100101", sponsor=root)
    buyer_b = _member("+918100100102", sponsor=sponsor_a)
    b_order = _paid_order(buyer_b, suffix="bfail")
    b_order.placement_status = Order.PlacementStatus.PENDING
    b_order.placement_deadline_at = timezone.now() - timedelta(minutes=1)
    b_order.save(update_fields=["placement_status", "placement_deadline_at"])

    with django_capture_on_commit_callbacks(execute=True):
        auto_place_pending_placements()

    buyer_b.refresh_from_db()
    assert not hasattr(buyer_b, "binary_node")
    sponsor_a.refresh_from_db()
    assert not hasattr(sponsor_a, "binary_node"), (
        "Sponsor must NOT be silently rooted by the auto-placer"
    )
    b_order.refresh_from_db()
    assert b_order.placement_status == Order.PlacementStatus.FAILED
    assert "not placed in the binary tree" in (
        b_order.placement_failure_reason or ""
    ).lower()


@pytest.mark.django_db
def test_auto_placer_retries_failed_after_sponsor_placed(
    system_config, django_capture_on_commit_callbacks
):
    """Once the sponsor is placed, the next auto-placer tick must retry the FAILED order and place the buyer."""
    root = _member("+918110110110")
    BinaryTreeService.place_member(root, None)
    sponsor_a = _member("+918110110111", sponsor=root)
    buyer_b = _member("+918110110112", sponsor=sponsor_a)
    b_order = _paid_order(buyer_b, suffix="bretry")
    b_order.placement_status = Order.PlacementStatus.FAILED
    b_order.placement_deadline_at = timezone.now() - timedelta(minutes=1)
    b_order.placement_failure_reason = "Sponsor is not placed in the binary tree."
    b_order.save(
        update_fields=[
            "placement_status",
            "placement_deadline_at",
            "placement_failure_reason",
        ]
    )

    BinaryTreeService.place_member_manual_leg(sponsor_a, root, BinaryNode.Position.LEFT)

    with django_capture_on_commit_callbacks(execute=True):
        auto_place_pending_placements()

    buyer_b.refresh_from_db()
    assert hasattr(buyer_b, "binary_node")
    b_order.refresh_from_db()
    assert b_order.placement_status == Order.PlacementStatus.PLACED_AUTO
    assert buyer_b.binary_node.parent is not None
