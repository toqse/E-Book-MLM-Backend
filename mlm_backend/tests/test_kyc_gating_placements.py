import pytest
from rest_framework.test import APIClient

from apps.agreements.models import MemberComplianceProfile
from apps.mlm_tree import placement as placement_mod
from apps.mlm_tree.services import BinaryTreeService
from apps.payments.models import Order
from apps.users.models import User
from apps.users.services import allocate_member_identity


def _member(phone: str, *, sponsor: User | None = None) -> User:
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


def _paid_order(user: User, *, suffix: str = "x") -> Order:
    return Order.objects.create(
        user=user,
        order_number=f"ORD-KYC-{user.id}-{suffix}",
        base_price="200",
        gst_amount="36",
        gateway_charge="5.72",
        total_amount="241.72",
        discount_amount="0",
        amount_paid="241.72",
        is_retail_purchase=False,
        status=Order.Status.PAID,
    )


def _make_sponsor_eligible(sponsor: User):
    sponsor.kyc_status = User.KYCStatus.VERIFIED
    sponsor.save(update_fields=["kyc_status"])
    MemberComplianceProfile.objects.create(user=sponsor)


@pytest.mark.django_db
def test_referral_list_pending_requires_kyc_verified(system_config):
    sponsor = _member("+919100000001")
    sponsor.is_member = True
    sponsor.save(update_fields=["is_member"])
    BinaryTreeService.place_member(sponsor, None)

    client = APIClient()
    client.force_authenticate(user=sponsor)

    r = client.get("/api/v1/user/referral/list/?pending_placement=true")
    assert r.status_code == 403
    body = r.json()
    assert body["success"] is False
    assert (body.get("errors") or {}).get("detail") in (
        "kyc_required",
        "compliance_profile_required",
    )

    _make_sponsor_eligible(sponsor)
    r2 = client.get("/api/v1/user/referral/list/?pending_placement=true")
    assert r2.status_code == 200, r2.content


@pytest.mark.django_db
def test_team_network_pending_include_requires_kyc_verified(system_config):
    sponsor = _member("+919100000002")
    sponsor.is_member = True
    sponsor.save(update_fields=["is_member"])
    BinaryTreeService.place_member(sponsor, None)

    client = APIClient()
    client.force_authenticate(user=sponsor)

    r = client.get("/api/v1/user/team/network/?include=summary,pending")
    assert r.status_code == 403
    body = r.json()
    assert body["success"] is False
    assert (body.get("errors") or {}).get("detail") in (
        "kyc_required",
        "compliance_profile_required",
    )

    _make_sponsor_eligible(sponsor)
    r2 = client.get("/api/v1/user/team/network/?include=summary,pending")
    assert r2.status_code == 200, r2.content
    assert "pending" in (r2.json().get("data") or {})


@pytest.mark.django_db
def test_place_direct_requires_kyc_verified_and_profile(system_config):
    sponsor = _member("+919100000003")
    sponsor.is_member = True
    sponsor.save(update_fields=["is_member"])
    BinaryTreeService.place_member(sponsor, None)

    buyer = _member("+919100000004", sponsor=sponsor)
    order = _paid_order(buyer, suffix="a")
    placement_mod.open_placement_queue_if_needed(order, buyer)

    client = APIClient()
    client.force_authenticate(user=sponsor)

    r = client.post(
        "/api/v1/user/tree/place-direct/",
        {"member_id": buyer.member_id, "leg": "LEFT"},
        format="json",
    )
    assert r.status_code == 403

    # Verified but no compliance profile still blocked per requirement
    sponsor.kyc_status = User.KYCStatus.VERIFIED
    sponsor.save(update_fields=["kyc_status"])
    r2 = client.post(
        "/api/v1/user/tree/place-direct/",
        {"member_id": buyer.member_id, "leg": "LEFT"},
        format="json",
    )
    assert r2.status_code == 403
    assert (r2.json().get("errors") or {}).get("detail") == "compliance_profile_required"

    # Verified + profile exists => KYC gate passes; placement may proceed
    MemberComplianceProfile.objects.create(user=sponsor)
    r3 = client.post(
        "/api/v1/user/tree/place-direct/",
        {"member_id": buyer.member_id, "leg": "LEFT"},
        format="json",
    )
    assert r3.status_code == 200, r3.content


@pytest.mark.django_db
def test_referral_list_all_requires_kyc_verified(system_config):
    sponsor = _member("+919100000011")
    sponsor.is_member = True
    sponsor.save(update_fields=["is_member"])
    BinaryTreeService.place_member(sponsor, None)

    client = APIClient()
    client.force_authenticate(user=sponsor)

    r = client.get("/api/v1/user/referral/list/")
    assert r.status_code == 403
    assert (r.json().get("errors") or {}).get("detail") in (
        "kyc_required",
        "compliance_profile_required",
    )


@pytest.mark.django_db
def test_team_network_tree_only_requires_kyc_verified(system_config):
    sponsor = _member("+919100000012")
    sponsor.is_member = True
    sponsor.save(update_fields=["is_member"])
    BinaryTreeService.place_member(sponsor, None)

    client = APIClient()
    client.force_authenticate(user=sponsor)

    r = client.get("/api/v1/user/team/network/?include=summary,tree")
    assert r.status_code == 403
    assert (r.json().get("errors") or {}).get("detail") in (
        "kyc_required",
        "compliance_profile_required",
    )


@pytest.mark.django_db
def test_earnings_bundle_requires_kyc_verified(system_config):
    sponsor = _member("+919100000013")
    sponsor.is_member = True
    sponsor.save(update_fields=["is_member"])
    BinaryTreeService.place_member(sponsor, None)

    client = APIClient()
    client.force_authenticate(user=sponsor)

    r = client.get("/api/v1/user/earnings/?include=overview,ledger&page=1")
    assert r.status_code == 403
    assert (r.json().get("errors") or {}).get("detail") in (
        "kyc_required",
        "compliance_profile_required",
    )


@pytest.mark.django_db
def test_commissions_milestones_requires_kyc_verified(system_config):
    sponsor = _member("+919100000014")
    sponsor.is_member = True
    sponsor.save(update_fields=["is_member"])
    BinaryTreeService.place_member(sponsor, None)

    client = APIClient()
    client.force_authenticate(user=sponsor)

    r = client.get("/api/v1/user/commissions/milestones/")
    assert r.status_code == 403
    assert (r.json().get("errors") or {}).get("detail") in (
        "kyc_required",
        "compliance_profile_required",
    )


@pytest.mark.django_db
def test_sponsor_slots_bundle_requires_kyc_verified(system_config):
    sponsor = _member("+919100000015")
    sponsor.is_member = True
    sponsor.save(update_fields=["is_member"])
    BinaryTreeService.place_member(sponsor, None)

    client = APIClient()
    client.force_authenticate(user=sponsor)

    r = client.get("/api/v1/user/sponsor-slots/bundle/")
    assert r.status_code == 403
    assert (r.json().get("errors") or {}).get("detail") in (
        "kyc_required",
        "compliance_profile_required",
    )


@pytest.mark.django_db
def test_payouts_bundle_requires_kyc_verified(system_config):
    sponsor = _member("+919100000016")
    sponsor.is_member = True
    sponsor.save(update_fields=["is_member"])
    BinaryTreeService.place_member(sponsor, None)

    client = APIClient()
    client.force_authenticate(user=sponsor)

    r = client.get("/api/v1/user/payouts/?movements=true")
    assert r.status_code == 403
    assert (r.json().get("errors") or {}).get("detail") in (
        "kyc_required",
        "compliance_profile_required",
    )
