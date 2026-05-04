from datetime import timedelta

import pytest
from decimal import Decimal
from django.utils import timezone
from rest_framework.test import APIClient

from apps.mlm_tree.services import BinaryTreeService
from apps.payments.models import Order
from apps.users.models import User
from apps.users.services import allocate_member_identity


def _user(phone: str, sponsor: User | None = None) -> User:
    mid, ref, link = allocate_member_identity()
    u = User(
        phone=phone,
        full_name="Member",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        sponsor=sponsor,
    )
    u.set_unusable_password()
    u.save()
    return u


def _paid_mlm_order(user: User, *, retail: bool = False) -> Order:
    return Order.objects.create(
        user=user,
        order_number=f"ORD-TEST-{user.id}-{retail}",
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("241.72"),
        is_retail_purchase=retail,
        status=Order.Status.PAID,
    )


@pytest.mark.django_db
def test_referral_list_pending_placement_filter(system_config):
    mid_s, r_s, l_s = allocate_member_identity()
    sponsor = User(
        phone="+919000000010",
        full_name="Sponsor",
        member_id=mid_s,
        referral_code=r_s,
        referral_link=l_s,
    )
    sponsor.set_unusable_password()
    sponsor.save()
    sponsor.is_member = True
    sponsor.save(update_fields=["is_member"])
    BinaryTreeService.place_member(sponsor, None)

    pending_user = _user("+919000000011", sponsor=sponsor)
    po = _paid_mlm_order(pending_user, retail=False)
    po.placement_status = Order.PlacementStatus.PENDING
    po.placement_deadline_at = timezone.now() + timedelta(hours=24)
    po.save(update_fields=["placement_status", "placement_deadline_at"])

    not_paid = _user("+919000000012", sponsor=sponsor)

    retail_only = _user("+919000000013", sponsor=sponsor)
    _paid_mlm_order(retail_only, retail=True)

    placed = _user("+919000000014", sponsor=sponsor)
    _paid_mlm_order(placed, retail=False)
    placed.is_member = True
    placed.save(update_fields=["is_member"])
    BinaryTreeService.place_member(placed, sponsor)

    client = APIClient()
    client.force_authenticate(user=sponsor)

    all_resp = client.get("/api/v1/user/referral/list/")
    assert all_resp.status_code == 200
    all_ids = {x["member_id"] for x in all_resp.json()["data"]["results"]}
    assert pending_user.member_id in all_ids
    assert not_paid.member_id in all_ids

    pend_resp = client.get("/api/v1/user/referral/list/?pending_placement=true")
    assert pend_resp.status_code == 200
    body = pend_resp.json()["data"]
    assert body.get("pending_placement") is True
    pending_ids = {x["member_id"] for x in body["results"]}
    assert pending_ids == {pending_user.member_id}
    assert body["count"] == 1
    row0 = next(x for x in body["results"] if x["member_id"] == pending_user.member_id)
    assert row0["placement_status"] == Order.PlacementStatus.PENDING
    assert row0["placement_order_id"] == po.id
    assert row0.get("placement_deadline_at")

    pend_resp_alt = client.get("/api/v1/user/referral/list/?pending_placement=1")
    assert pend_resp_alt.json()["data"]["count"] == 1
