"""Admin PATCH /api/v1/admin/users/<id>/ — compliance field normalization."""

import pytest
from rest_framework.test import APIClient

from apps.agreements.models import MemberComplianceProfile
from apps.users.models import User
from apps.users.services import allocate_member_identity


def _admin():
    return User.objects.create_user(
        login_identifier="patch-admin@test.dev",
        password="pw",
        email="patch-admin@test.dev",
        full_name="Patch Admin",
        member_id="PATCHADM1",
        referral_code="PATCH01",
        referral_link="http://localhost/join?ref=PATCH01",
        role=User.Role.SUPER_ADMIN,
        is_staff=True,
    )


def _member() -> User:
    mid, ref, link = allocate_member_identity()
    u = User(
        phone="+919876543210",
        full_name="Patch Member",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        is_member=True,
    )
    u.set_unusable_password()
    u.save()
    return u


@pytest.mark.django_db
def test_admin_patch_user_accepts_display_gender_and_iso_dob(system_config):
    admin = _admin()
    member = _member()
    client = APIClient()
    client.force_authenticate(user=admin)

    r = client.patch(
        f"/api/v1/admin/users/{member.pk}/",
        {
            "kyc_status": "VERIFIED",
            "pan_number": "GQFPK4771A",
            "aadhaar_number": "218503021542",
            "date_of_birth": "2006-11-12",
            "gender": "Male",
            "full_address": "Test address",
            "city": "Kottayam",
            "pin_code": "652321",
            "state": "Kerala",
            "country": "India",
        },
        format="json",
    )
    assert r.status_code == 200, r.content
    assert r.json()["success"] is True

    member.refresh_from_db()
    profile = MemberComplianceProfile.objects.get(user=member)
    assert profile.gender == MemberComplianceProfile.Gender.M
    assert profile.date_of_birth.isoformat() == "2006-11-12"
    assert member.kyc_status == User.KYCStatus.VERIFIED
    assert member.kyc_first_approved_at is not None


@pytest.mark.django_db
def test_admin_patch_user_rejects_invalid_gender(system_config):
    admin = _admin()
    member = _member()
    client = APIClient()
    client.force_authenticate(user=admin)

    r = client.patch(
        f"/api/v1/admin/users/{member.pk}/",
        {"gender": "Unknown"},
        format="json",
    )
    assert r.status_code == 400
    assert r.json()["success"] is False
