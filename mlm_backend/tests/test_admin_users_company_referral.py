import pytest
from rest_framework.test import APIClient

from apps.users.models import User
from apps.users.services import allocate_member_identity


def _admin():
    return User.objects.create_user(
        login_identifier="users-admin@test.dev",
        password="pw",
        email="users-admin@test.dev",
        full_name="Users Admin",
        member_id="USRADM01",
        referral_code="USRADM01",
        referral_link="http://localhost/join?ref=USRADM01",
        role=User.Role.SUPER_ADMIN,
        is_staff=True,
    )


def _member(phone: str, *, name: str = "Member", **extra) -> User:
    mid, ref, link = allocate_member_identity()
    u = User(
        phone=phone,
        full_name=name,
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        is_member=True,
        **extra,
    )
    u.set_unusable_password()
    u.save()
    return u


@pytest.mark.django_db
def test_admin_users_company_referral_filter_and_tab_count(system_config):
    admin = _admin()
    company_user = _member(
        "+919911111111",
        name="Company Signup",
        signup_referral_code="Admin",
        joined_via_company_referral=True,
        sponsor=admin,
    )
    _member(
        "+919922222222",
        name="Member Signup",
        signup_referral_code="MEMBER01",
        joined_via_company_referral=False,
    )

    client = APIClient()
    client.force_authenticate(user=admin)

    r = client.get("/api/v1/admin/users/")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["tab_counts"]["company_referral"] == 1

    r2 = client.get("/api/v1/admin/users/?joined_via_company_referral=true")
    assert r2.status_code == 200
    filtered = r2.json()["data"]
    assert filtered["count"] == 1
    assert filtered["results"][0]["member_id"] == company_user.member_id
    assert filtered["results"][0]["joined_via_company_referral"] is True
    assert filtered["results"][0]["signup_referral_code"] == "Admin"
    assert filtered["results"][0]["referrer"] == {
        "member_id": admin.member_id,
        "full_name": admin.full_name,
    }
