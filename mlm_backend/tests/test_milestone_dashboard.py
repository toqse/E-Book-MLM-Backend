from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.agreements.models import MemberComplianceProfile
from apps.commissions.models import MilestoneRecord
from apps.commissions.services.milestone_payload import build_user_milestones_dashboard
from apps.users.models import User
from apps.users.services import allocate_member_identity


@pytest.mark.django_db
def test_milestone_dashboard_progress_remaining_and_summary(system_config):
    mid, ref, link = allocate_member_identity()
    u = User(
        phone="+918000000099",
        full_name="Milestone User",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        direct_referral_count=42,
    )
    u.set_unusable_password()
    u.save()

    MilestoneRecord.objects.create(
        user=u,
        milestone_referrals=10,
        bonus_amount=Decimal("300"),
        tds_deducted=Decimal("0"),
        net_bonus=Decimal("300"),
        status="CREDITED",
    )
    MilestoneRecord.objects.create(
        user=u,
        milestone_referrals=25,
        bonus_amount=Decimal("600"),
        tds_deducted=Decimal("0"),
        net_bonus=Decimal("600"),
        status="CREDITED",
    )

    data = build_user_milestones_dashboard(u)
    assert data["qualifying_referrals"]["count"] == 42
    assert data["summary"]["milestones_completed"]["current"] == 2
    assert data["summary"]["milestones_completed"]["total"] == 5
    assert data["summary"]["bonus_earned_so_far"] == "900.00"
    assert data["summary"]["remaining_potential_bonus_gross"] == "3950.00"

    by_tier = {t["tier"]: t for t in data["tiers"]}
    assert by_tier["T1"]["status"] == "UNLOCKED"
    assert by_tier["T2"]["status"] == "UNLOCKED"
    assert by_tier["T3"]["status"] == "IN_PROGRESS"
    assert by_tier["T3"]["remaining_to_threshold"] == 8
    assert by_tier["T3"]["progress"]["percent_toward_tier"] == 84
    assert by_tier["T4"]["status"] == "LOCKED"
    assert by_tier["T5"]["status"] == "LOCKED"

    assert len(data["history"]) == 2
    assert "bonus_gross" in data["history"][0]
    assert "earned_at" in data["history"][0]


@pytest.mark.django_db
def test_milestone_dashboard_missed_or_blocked_when_no_record(system_config):
    mid, ref, link = allocate_member_identity()
    u = User(
        phone="+918000000098",
        full_name="Missed Tier",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        direct_referral_count=50,
    )
    u.set_unusable_password()
    u.save()

    data = build_user_milestones_dashboard(u)
    by_tier = {t["tier"]: t for t in data["tiers"]}
    assert by_tier["T3"]["status"] == "MISSED_OR_BLOCKED"
    assert by_tier["T3"]["status_reason"] == "no_record_at_threshold"


@pytest.mark.django_db
def test_milestones_endpoint_envelope(system_config):
    mid, ref, link = allocate_member_identity()
    u = User(
        phone="+918000000097",
        full_name="API Milestone",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        direct_referral_count=0,
    )
    u.set_unusable_password()
    u.save()
    u.kyc_status = User.KYCStatus.VERIFIED
    u.save(update_fields=["kyc_status"])
    MemberComplianceProfile.objects.create(user=u)

    client = APIClient()
    client.force_authenticate(user=u)
    r = client.get("/api/v1/user/commissions/milestones/")
    assert r.status_code == 200
    body = r.json()
    assert body.get("success") is True
    d = body["data"]
    assert "tiers" in d and len(d["tiers"]) == 5
    assert d["tiers"][0]["status"] == "IN_PROGRESS"
    assert "cap_context" in d
