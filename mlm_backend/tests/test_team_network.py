import pytest
from django.test.utils import CaptureQueriesContext
from django.db import connection
from rest_framework.test import APIClient

from apps.agreements.models import MemberComplianceProfile
from apps.mlm_tree.services import BinaryTreeService
from apps.users.models import User
from apps.users.services import allocate_member_identity


@pytest.mark.django_db
def test_team_network_default_include(system_config):
    mid1, r1, l1 = allocate_member_identity()
    sponsor = User(
        phone="+919000001001",
        full_name="Sponsor One",
        member_id=mid1,
        referral_code=r1,
        referral_link=l1,
    )
    sponsor.set_unusable_password()
    sponsor.save()
    sponsor.is_member = True
    sponsor.save()
    sponsor.kyc_status = User.KYCStatus.VERIFIED
    sponsor.save(update_fields=["kyc_status"])
    MemberComplianceProfile.objects.create(user=sponsor)
    BinaryTreeService.place_member(sponsor, None)

    client = APIClient()
    client.force_authenticate(user=sponsor)
    r = client.get("/api/v1/user/team/network/")
    assert r.status_code == 200
    body = r.json()["data"]
    assert "summary" in body
    assert "pending" in body
    assert "tree" in body
    assert "roster" not in body
    assert body["summary"]["total_referrals"] == 0
    assert body["summary"]["left_leg_count"] == 0
    assert body["pending"]["count"] == 0
    assert body["tree"]["root"]["member_id"] == sponsor.member_id
    assert "initials" in body["tree"]["root"]


@pytest.mark.django_db
def test_team_network_include_roster_and_leg_counts(system_config):
    mid1, r1, l1 = allocate_member_identity()
    sponsor = User(
        phone="+919000001002",
        full_name="Alpha Beta",
        member_id=mid1,
        referral_code=r1,
        referral_link=l1,
    )
    sponsor.set_unusable_password()
    sponsor.save()
    sponsor.is_member = True
    sponsor.save()
    sponsor.kyc_status = User.KYCStatus.VERIFIED
    sponsor.save(update_fields=["kyc_status"])
    MemberComplianceProfile.objects.create(user=sponsor)
    BinaryTreeService.place_member(sponsor, None)

    mid2, r2, l2 = allocate_member_identity()
    left_child = User(
        phone="+919000001003",
        full_name="Left Child",
        member_id=mid2,
        referral_code=r2,
        referral_link=l2,
        sponsor=sponsor,
    )
    left_child.set_unusable_password()
    left_child.save()
    left_child.is_member = True
    left_child.save()
    BinaryTreeService.place_member(left_child, sponsor)

    mid3, r3, l3 = allocate_member_identity()
    right_child = User(
        phone="+919000001004",
        full_name="Right Child",
        member_id=mid3,
        referral_code=r3,
        referral_link=l3,
        sponsor=sponsor,
    )
    right_child.set_unusable_password()
    right_child.save()
    right_child.is_member = True
    right_child.save()
    BinaryTreeService.place_member(right_child, sponsor)

    client = APIClient()
    client.force_authenticate(user=sponsor)
    r = client.get("/api/v1/user/team/network/?include=summary,roster&roster_page_size=10")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["summary"]["left_leg_count"] == 1
    assert data["summary"]["right_leg_count"] == 1
    assert "roster" in data
    assert data["roster"]["count"] == 2
    assert data["roster"]["filter_totals"]["all"] == 2
    assert data["roster"]["filter_totals"]["left_leg"] == 1
    assert data["roster"]["filter_totals"]["right_leg"] == 1


@pytest.mark.django_db
def test_team_network_roster_pagination(system_config):
    mid1, r1, l1 = allocate_member_identity()
    sponsor = User(
        phone="+919000001005",
        full_name="S",
        member_id=mid1,
        referral_code=r1,
        referral_link=l1,
    )
    sponsor.set_unusable_password()
    sponsor.save()
    sponsor.is_member = True
    sponsor.save()
    sponsor.kyc_status = User.KYCStatus.VERIFIED
    sponsor.save(update_fields=["kyc_status"])
    MemberComplianceProfile.objects.create(user=sponsor)
    BinaryTreeService.place_member(sponsor, None)

    mid2, r2, l2 = allocate_member_identity()
    c = User(
        phone="+919000001006",
        full_name="C",
        member_id=mid2,
        referral_code=r2,
        referral_link=l2,
        sponsor=sponsor,
    )
    c.set_unusable_password()
    c.save()
    c.is_member = True
    c.save()
    BinaryTreeService.place_member(c, sponsor)

    client = APIClient()
    client.force_authenticate(user=sponsor)
    r = client.get("/api/v1/user/team/network/roster/?page_size=1&page=1")
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["count"] == 1
    assert len(d["results"]) == 1


@pytest.mark.django_db
def test_referral_list_pending_has_leg_meta(system_config):
    mid1, r1, l1 = allocate_member_identity()
    sponsor = User(
        phone="+919000001007",
        full_name="S",
        member_id=mid1,
        referral_code=r1,
        referral_link=l1,
    )
    sponsor.set_unusable_password()
    sponsor.save()
    sponsor.is_member = True
    sponsor.save()
    sponsor.kyc_status = User.KYCStatus.VERIFIED
    sponsor.save(update_fields=["kyc_status"])
    MemberComplianceProfile.objects.create(user=sponsor)
    BinaryTreeService.place_member(sponsor, None)

    client = APIClient()
    client.force_authenticate(user=sponsor)
    r = client.get("/api/v1/user/referral/list/?pending_placement=true")
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["viewer_leg_counts"] == {"left": 0, "right": 0}
    assert d["suggested_leg"] in ("LEFT", "RIGHT")


@pytest.mark.django_db
def test_team_network_query_budget(system_config):
    mid1, r1, l1 = allocate_member_identity()
    sponsor = User(
        phone="+919000001008",
        full_name="S",
        member_id=mid1,
        referral_code=r1,
        referral_link=l1,
    )
    sponsor.set_unusable_password()
    sponsor.save()
    sponsor.is_member = True
    sponsor.save()
    sponsor.kyc_status = User.KYCStatus.VERIFIED
    sponsor.save(update_fields=["kyc_status"])
    MemberComplianceProfile.objects.create(user=sponsor)
    BinaryTreeService.place_member(sponsor, None)

    for i in range(3):
        mid, rc, lk = allocate_member_identity()
        u = User(
            phone=f"+9190000011{i:02d}",
            full_name=f"M{i}",
            member_id=mid,
            referral_code=rc,
            referral_link=lk,
            sponsor=sponsor,
        )
        u.set_unusable_password()
        u.save()
        u.is_member = True
        u.save()
        BinaryTreeService.place_member(u, sponsor)

    client = APIClient()
    client.force_authenticate(user=sponsor)
    with CaptureQueriesContext(connection) as ctx:
        r = client.get(
            "/api/v1/user/team/network/?include=summary,pending,tree,roster&roster_page_size=5"
        )
    assert r.status_code == 200
    assert len(ctx.captured_queries) < 40
