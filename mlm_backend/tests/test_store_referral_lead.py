from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.models import OTPRecord, StoreReferralLead
from apps.users.models import User
from apps.users.services import allocate_member_identity


@pytest.fixture
def sponsor_user(db):
    mid, ref, link = allocate_member_identity()
    u = User(
        phone="+919888877766",
        email="sponsor@test.dev",
        full_name="Sponsor User",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        account_status=User.AccountStatus.ACTIVE,
    )
    u.set_unusable_password()
    u.save()
    return u


LEAD_PHONE = "+919777766655"


@pytest.mark.django_db
def test_store_referral_lead_creates_record(sponsor_user):
    client = APIClient()
    resp = client.post(
        "/api/v1/auth/store-referral-lead/",
        {
            "phone": LEAD_PHONE,
            "referral_code": sponsor_user.referral_code,
            "platform": "android",
        },
        format="json",
    )
    assert resp.status_code == 200
    lead = StoreReferralLead.objects.get(phone=LEAD_PHONE)
    assert lead.referral_code == sponsor_user.referral_code.upper()
    assert lead.platform == StoreReferralLead.Platform.ANDROID


@pytest.mark.django_db
def test_store_referral_lead_upserts_by_phone(sponsor_user):
    mid2, ref2, link2 = allocate_member_identity()
    other = User(
        phone="+919666655544",
        email="other@test.dev",
        full_name="Other Sponsor",
        member_id=mid2,
        referral_code=ref2,
        referral_link=link2,
        account_status=User.AccountStatus.ACTIVE,
    )
    other.set_unusable_password()
    other.save()

    client = APIClient()
    client.post(
        "/api/v1/auth/store-referral-lead/",
        {
            "phone": LEAD_PHONE,
            "referral_code": sponsor_user.referral_code,
            "platform": "android",
        },
        format="json",
    )
    client.post(
        "/api/v1/auth/store-referral-lead/",
        {
            "phone": LEAD_PHONE,
            "referral_code": other.referral_code,
            "platform": "ios",
        },
        format="json",
    )
    lead = StoreReferralLead.objects.get(phone=LEAD_PHONE)
    assert lead.referral_code == other.referral_code.upper()
    assert lead.platform == StoreReferralLead.Platform.IOS


@pytest.mark.django_db
def test_store_referral_lead_rejects_invalid_code():
    client = APIClient()
    resp = client.post(
        "/api/v1/auth/store-referral-lead/",
        {
            "phone": LEAD_PHONE,
            "referral_code": "NOTVALID",
            "platform": "android",
        },
        format="json",
    )
    assert resp.status_code == 400
    assert StoreReferralLead.objects.count() == 0


@pytest.mark.django_db
def test_store_referral_lead_rejects_registered_phone(sponsor_user, member_user):
    client = APIClient()
    resp = client.post(
        "/api/v1/auth/store-referral-lead/",
        {
            "phone": member_user.phone,
            "referral_code": sponsor_user.referral_code,
            "platform": "android",
        },
        format="json",
    )
    assert resp.status_code == 400
    assert "already exists" in resp.json()["message"].lower()


@pytest.mark.django_db
def test_referral_by_phone_returns_stored_code(sponsor_user):
    StoreReferralLead.objects.create(
        phone=LEAD_PHONE,
        referral_code=sponsor_user.referral_code.upper(),
        platform=StoreReferralLead.Platform.ANDROID,
        expires_at=timezone.now() + timedelta(days=30),
    )
    client = APIClient()
    resp = client.get("/api/v1/auth/referral-by-phone/", {"phone": LEAD_PHONE})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["referral_code"] == sponsor_user.referral_code.upper()
    assert data["referrer_name"] == sponsor_user.full_name
    assert data["referrer_id"] == sponsor_user.member_id
    assert data["phone_registered"] is False


@pytest.mark.django_db
def test_referral_by_phone_returns_null_when_missing():
    client = APIClient()
    resp = client.get("/api/v1/auth/referral-by-phone/", {"phone": LEAD_PHONE})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["referral_code"] is None
    assert data["phone_registered"] is False


@pytest.mark.django_db
def test_referral_by_phone_returns_null_when_expired(sponsor_user):
    StoreReferralLead.objects.create(
        phone=LEAD_PHONE,
        referral_code=sponsor_user.referral_code.upper(),
        platform=StoreReferralLead.Platform.ANDROID,
        expires_at=timezone.now() - timedelta(minutes=1),
    )
    client = APIClient()
    resp = client.get("/api/v1/auth/referral-by-phone/", {"phone": LEAD_PHONE})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["referral_code"] is None
    assert data["phone_registered"] is False
    assert not StoreReferralLead.objects.filter(phone=LEAD_PHONE).exists()


@pytest.mark.django_db
def test_referral_by_phone_returns_phone_registered_for_existing_user(sponsor_user, member_user):
    StoreReferralLead.objects.create(
        phone=member_user.phone,
        referral_code=sponsor_user.referral_code.upper(),
        platform=StoreReferralLead.Platform.ANDROID,
        expires_at=timezone.now() + timedelta(days=30),
    )
    client = APIClient()
    resp = client.get("/api/v1/auth/referral-by-phone/", {"phone": member_user.phone})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["referral_code"] is None
    assert data["phone_registered"] is True


@pytest.mark.django_db
def test_store_referral_lead_consumed_on_register(sponsor_user):
    User.objects.create_superuser(
        "company-admin@test.dev",
        "pw",
        full_name="Platform Admin",
        email="company-admin@test.dev",
    )
    StoreReferralLead.objects.create(
        phone=LEAD_PHONE,
        referral_code=sponsor_user.referral_code.upper(),
        platform=StoreReferralLead.Platform.ANDROID,
        expires_at=timezone.now() + timedelta(days=30),
    )

    client = APIClient()
    send = client.post(
        "/api/v1/auth/register/send-otp/",
        {
            "phone": LEAD_PHONE,
            "email": "lead-user@test.dev",
            "full_name": "Lead User",
            "referral_code": sponsor_user.referral_code,
        },
        format="json",
    )
    assert send.status_code == 200
    otp = OTPRecord.objects.filter(phone=LEAD_PHONE, purpose=OTPRecord.Purpose.REGISTER).latest(
        "id"
    ).otp_code

    finish = client.post(
        "/api/v1/auth/verify-otp-register/",
        {"phone": LEAD_PHONE, "otp_code": otp},
        format="json",
    )
    assert finish.status_code == 200
    assert not StoreReferralLead.objects.filter(phone=LEAD_PHONE).exists()
    user = User.objects.get(phone=LEAD_PHONE)
    assert user.sponsor_id == sponsor_user.id


@pytest.mark.django_db
def test_public_app_version_includes_store_urls():
    from apps.admin_panel.utils import get_system_config

    cfg = get_system_config()
    cfg.play_store_url = "https://play.google.com/store/apps/details?id=test"
    cfg.app_store_url = "https://apps.apple.com/app/id123"
    cfg.save()

    client = APIClient()
    resp = client.get("/api/v1/app-version/")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["play_store_url"] == cfg.play_store_url
    assert data["app_store_url"] == cfg.app_store_url
