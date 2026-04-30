from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.models import OTPRecord
from apps.authentication.otp import normalize_otp_code, verify_otp

User = get_user_model()


REGISTER_PHONE = "+919111111111"
REGISTER_EMAIL = "newuser@test.dev"


@pytest.mark.django_db
def test_register_full_payload_and_company_referral():
    User.objects.create_superuser(
        "company-admin@test.dev",
        "pw",
        full_name="Platform Admin",
        email="company-admin@test.dev",
    )

    client = APIClient()

    send = client.post(
        "/api/v1/auth/register/send-otp/",
        {
            "phone": REGISTER_PHONE,
            "email": REGISTER_EMAIL,
            "full_name": "New User",
            "referral_code": "Admin",
        },
        format="json",
    )
    assert send.status_code == 200
    otp_rec = OTPRecord.objects.filter(
        purpose=OTPRecord.Purpose.REGISTER,
        phone=REGISTER_PHONE,
    ).latest("id")
    otp = otp_rec.otp_code

    chk = client.post(
        "/api/v1/auth/validate-referral/",
        {"referral_code": "Admin"},
        format="json",
    )
    assert chk.status_code == 200
    assert chk.json()["data"]["sponsor_name"] == "Platform Admin"

    finish = client.post(
        "/api/v1/auth/verify-otp-register/",
        {
            "phone": REGISTER_PHONE,
            "otp_code": otp,
        },
        format="json",
    )
    assert finish.status_code == 200, finish.content
    data = finish.json()["data"]
    assert data["tokens"]["access"]
    assert data["user"]["referral_code"] != "Admin"
    u = User.objects.get(phone=REGISTER_PHONE)
    assert u.full_name == "New User"
    assert u.email == REGISTER_EMAIL
    assert u.sponsor is not None


@pytest.mark.django_db
def test_register_send_otp_validation_message_echoes_field_error():
    client = APIClient()
    r = client.post(
        "/api/v1/auth/register/send-otp/",
        {
            "phone": "8589960592",
            "full_name": "X",
            "referral_code": "Admin",
        },
        format="json",
    )
    assert r.status_code == 400
    body = r.json()
    assert body["message"] == "Phone must be with country code"
    assert body["errors"]["phone"] == ["Phone must be with country code"]


LOGIN_PHONE = "+919000000001"


def test_normalize_otp_code_preserves_or_restores_leading_zeros():
    assert normalize_otp_code("084532") == "084532"
    assert normalize_otp_code(84532) == "084532"
    assert normalize_otp_code("084 532") == "084532"
    assert normalize_otp_code(123456) == "123456"
    assert normalize_otp_code("1234567") is None


@pytest.mark.django_db
def test_verify_otp_matches_when_client_sends_numeric_without_leading_zero():
    exp = timezone.now() + timedelta(minutes=10)
    OTPRecord.objects.create(
        phone=LOGIN_PHONE,
        email=None,
        otp_code="084532",
        purpose=OTPRecord.Purpose.LOGIN,
        expires_at=exp,
    )
    rec, err = verify_otp(
        phone=LOGIN_PHONE, email=None, code=84532, purpose=OTPRecord.Purpose.LOGIN
    )
    assert err is None and rec is not None


@pytest.mark.django_db
def test_verify_otp_matches_code_not_only_latest_send():
    """After multiple LOGIN sends, verifying with an older still-valid OTP must succeed."""
    exp = timezone.now() + timedelta(minutes=10)
    OTPRecord.objects.create(
        phone=LOGIN_PHONE,
        email=None,
        otp_code="111111",
        purpose=OTPRecord.Purpose.LOGIN,
        expires_at=exp,
    )
    OTPRecord.objects.create(
        phone=LOGIN_PHONE,
        email=None,
        otp_code="222222",
        purpose=OTPRecord.Purpose.LOGIN,
        expires_at=exp,
    )
    rec, err = verify_otp(
        phone=LOGIN_PHONE, email=None, code="111111", purpose=OTPRecord.Purpose.LOGIN
    )
    assert err is None
    assert rec is not None
    assert rec.otp_code == "111111"


@pytest.mark.django_db
def test_login_send_otp_requires_e164_country_code():
    client = APIClient()
    r = client.post(
        "/api/v1/auth/send-otp/",
        {"phone": "9000000001", "purpose": "LOGIN"},
        format="json",
    )
    assert r.status_code == 400
    body = r.json()
    assert body["message"] == "Phone must be with country code"


@pytest.mark.django_db
def test_login_send_otp_rejects_truncated_indian_mobile():
    """+91 followed by nine digits fails; verify must use the same normalized number as send."""
    client = APIClient()
    r = client.post(
        "/api/v1/auth/send-otp/",
        {"phone": "+91858996093", "purpose": "LOGIN"},
        format="json",
    )
    assert r.status_code == 400
    assert "Indian mobile" in r.json().get("message", "")


@pytest.mark.django_db
def test_send_otp_rate_limit():
    client = APIClient()
    for _ in range(3):
        r = client.post(
            "/api/v1/auth/send-otp/",
            {"phone": LOGIN_PHONE, "purpose": "LOGIN"},
            format="json",
        )
        assert r.status_code == 200
    r = client.post(
        "/api/v1/auth/send-otp/",
        {"phone": LOGIN_PHONE, "purpose": "LOGIN"},
        format="json",
    )
    assert r.status_code == 429


@pytest.mark.django_db
def test_verify_otp_login_returns_role_user():
    login_phone = "+919333333333"
    User.objects.create_user(
        login_identifier=login_phone,
        password="pw",
        phone=login_phone,
        full_name="Member Login",
        member_id="MBR000201",
        referral_code="MBR201",
        referral_link="http://localhost:3000/join?ref=MBR201",
        role=User.Role.MEMBER,
        is_staff=False,
    )
    client = APIClient()
    send = client.post(
        "/api/v1/auth/send-otp/",
        {"phone": login_phone, "purpose": "LOGIN"},
        format="json",
    )
    assert send.status_code == 200
    otp = OTPRecord.objects.filter(
        phone=login_phone,
        purpose=OTPRecord.Purpose.LOGIN,
    ).latest("id").otp_code
    verify = client.post(
        "/api/v1/auth/verify-otp-login/",
        {"phone": login_phone, "otp_code": otp},
        format="json",
    )
    assert verify.status_code == 200, verify.content
    assert verify.json()["data"]["role"] == "user"


@pytest.mark.django_db
def test_admin_otp_login_with_phone():
    admin_phone = "+919222222222"
    User.objects.create_user(
        login_identifier=admin_phone,
        password="pw",
        phone=admin_phone,
        full_name="Admin Phone",
        member_id="ADM000101",
        referral_code="ADMPH01",
        referral_link="http://localhost:3000/join?ref=ADMPH01",
        role=User.Role.SUPER_ADMIN,
        is_staff=True,
    )
    client = APIClient()
    send = client.post(
        "/api/v1/admin/auth/send-otp/",
        {"phone": admin_phone, "purpose": "ADMIN_LOGIN"},
        format="json",
    )
    assert send.status_code == 200
    otp = OTPRecord.objects.filter(
        phone=admin_phone,
        purpose=OTPRecord.Purpose.ADMIN_LOGIN,
    ).latest("id").otp_code
    verify = client.post(
        "/api/v1/admin/auth/verify-otp/",
        {"phone": admin_phone, "otp_code": otp},
        format="json",
    )
    assert verify.status_code == 200, verify.content
    assert verify.json()["data"]["tokens"]["access"]
    assert verify.json()["data"]["role"] == "admin"


@pytest.mark.django_db
def test_admin_otp_login_with_email():
    admin_email = "admin.otp@test.dev"
    User.objects.create_user(
        login_identifier=admin_email,
        password="pw",
        email=admin_email,
        full_name="Admin Email",
        member_id="ADM000102",
        referral_code="ADMEM01",
        referral_link="http://localhost:3000/join?ref=ADMEM01",
        role=User.Role.SUPPORT,
        is_staff=True,
    )
    client = APIClient()
    send = client.post(
        "/api/v1/admin/auth/send-otp/",
        {"email": admin_email, "purpose": "ADMIN_LOGIN"},
        format="json",
    )
    assert send.status_code == 200
    otp = OTPRecord.objects.filter(
        email=admin_email,
        purpose=OTPRecord.Purpose.ADMIN_LOGIN,
    ).latest("id").otp_code
    verify = client.post(
        "/api/v1/admin/auth/verify-otp/",
        {"email": admin_email, "otp_code": otp},
        format="json",
    )
    assert verify.status_code == 200, verify.content
    assert verify.json()["data"]["tokens"]["access"]
    assert verify.json()["data"]["role"] == "admin"
