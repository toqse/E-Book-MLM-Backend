from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.agreements.models import MemberComplianceProfile
from apps.authentication.models import OTPRecord
from apps.authentication.otp import normalize_otp_code, verify_otp
from apps.courses.models import EBook
from apps.payments.models import Order

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
    assert data["is_book_purchased"] is False
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
    assert "Invalid" in r.json().get("message", "")


@pytest.mark.django_db
def test_login_send_otp_unknown_phone_returns_404():
    client = APIClient()
    r = client.post(
        "/api/v1/auth/send-otp/",
        {"phone": "+919888888888", "purpose": "LOGIN"},
        format="json",
    )
    assert r.status_code == 404
    assert r.json()["message"] == "User not found"


@pytest.mark.django_db
def test_send_otp_rate_limit():
    User.objects.create_user(
        login_identifier=LOGIN_PHONE,
        password="pw",
        phone=LOGIN_PHONE,
        full_name="Rate Limit User",
        member_id="MBR000401",
        referral_code="MBR401",
        referral_link="http://localhost:3000/join?ref=MBR401",
        role=User.Role.MEMBER,
        is_staff=False,
    )
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
    body = verify.json()["data"]
    assert body["role"] == "user"
    assert body["is_book_purchased"] is False


@pytest.mark.django_db
def test_verify_otp_login_is_book_purchased_true_when_paid_ebook_order():
    login_phone = "+919444444444"
    u = User.objects.create_user(
        login_identifier=login_phone,
        password="pw",
        phone=login_phone,
        full_name="Book Buyer",
        member_id="MBR000301",
        referral_code="MBR301",
        referral_link="http://localhost:3000/join?ref=MBR301",
        role=User.Role.MEMBER,
        is_staff=False,
    )
    book = EBook.objects.create(
        title="OTP Login Book",
        slug="otp-login-book",
        category="Business",
        description="d",
        pages_count=10,
        language="English",
        price=Decimal("100.00"),
        status=EBook.Status.PUBLISHED,
        file_url="https://example.com/x.pdf",
        is_active=True,
    )
    Order.objects.create(
        user=u,
        ebook=book,
        order_number="ORD-OTP-BOOK-1",
        base_price=Decimal("100"),
        gst_amount=Decimal("18"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("123.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("123.72"),
        status=Order.Status.PAID,
    )
    client = APIClient()
    assert (
        client.post(
            "/api/v1/auth/send-otp/",
            {"phone": login_phone, "purpose": "LOGIN"},
            format="json",
        ).status_code
        == 200
    )
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
    assert verify.json()["data"]["is_book_purchased"] is True


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


@pytest.mark.django_db
def test_me_includes_personal_and_member_info_blocks():
    u = User.objects.create_user(
        login_identifier="+919444444444",
        password="pw",
        phone="+919444444444",
        email="me@test.dev",
        full_name="Me User",
        member_id="MBR000301",
        referral_code="MBR301",
        referral_link="http://localhost:3000/join?ref=MBR301",
        role=User.Role.MEMBER,
        is_staff=False,
    )
    MemberComplianceProfile.objects.create(
        user=u,
        date_of_birth="1992-03-15",
        gender=MemberComplianceProfile.Gender.M,
        full_address="42, MG Road, Ernakulam",
        state="Kerala",
        pin_code="682001",
        country="India",
    )
    client = APIClient()
    client.force_authenticate(user=u)
    resp = client.get("/api/v1/auth/me/")
    assert resp.status_code == 200, resp.content
    data = resp.json()["data"]
    assert data["is_book_purchased"] is False
    assert data["personal_information"]["full_name"] == "Me User"
    assert data["personal_information"]["email_address"] == "me@test.dev"
    assert data["personal_information"]["mobile_number"] == "+919444444444"
    assert data["personal_information"]["date_of_birth"] == "15/03/1992"
    assert data["personal_information"]["gender"] == "Male"
    assert data["member_information"]["member_id"] == "MBR000301"
    assert data["member_information"]["address"] == "42, MG Road, Ernakulam"
    assert data["member_information"]["state"] == "Kerala"
    assert data["member_information"]["pin_code"] == "682001"
    assert data["member_information"]["country"] == "India"

    assert data["display"]["profile_initial"] == "M"
    assert data["display"]["avatar_url"] is None

    assert data["account_status"]["account_status"] == User.AccountStatus.ACTIVE
    assert data["account_status"]["kyc_status"] == User.KYCStatus.PENDING
    assert data["account_status"]["pan_submitted"] is False
    assert data["account_status"]["withdrawals_blocked"] is True
    assert data["account_status"]["referral_link"] == "http://localhost:3000/join?ref=MBR301"
    assert data["account_status"]["referral_link_active"] is True

    assert "tds_rate_percent" in data["tax_withholding"]
    assert "reason" in data["tax_withholding"]

    assert "current_band" in data["withdrawal_band"]
    assert isinstance(data["withdrawal_band"]["bands"], list)

    assert "limit" in data["earning_cap"]
    assert "used" in data["earning_cap"]
    assert "used_percent" in data["earning_cap"]
    assert "remaining" in data["earning_cap"]

    assert "message" in data["kyc_notice"]

    assert data["sponsor"] is None

    assert data["binary_placement"]["position"] is None
    assert data["binary_placement"]["level"] is None

    assert data["team_legs"]["left_leg_count"] == 0
    assert data["team_legs"]["right_leg_count"] == 0
    assert data["team_legs"]["weaker_leg"] in ("LEFT", "RIGHT")


@pytest.mark.django_db
def test_me_patch_updates_user_and_compliance_profile_fields():
    u = User.objects.create_user(
        login_identifier="+919444444445",
        password="pw",
        phone="+919444444445",
        email="before@test.dev",
        full_name="Before Name",
        member_id="MBR000302",
        referral_code="MBR302",
        referral_link="http://localhost:3000/join?ref=MBR302",
        role=User.Role.MEMBER,
        is_staff=False,
    )
    client = APIClient()
    client.force_authenticate(user=u)
    payload = {
        "full_name": "After Name",
        "email": "after@test.dev",
        "date_of_birth": "17/08/1996",
        "gender": "Male",
        "address": "44, MG Road, Ernakulam",
        "city": "Kochi",
        "pin_code": "682001",
        "state": "Kerala",
        "country": "India",
    }
    resp = client.patch("/api/v1/auth/me/", payload, format="json")
    assert resp.status_code == 200, resp.content
    data = resp.json()["data"]
    assert data["personal_information"]["full_name"] == "After Name"
    assert data["personal_information"]["email_address"] == "after@test.dev"
    assert data["personal_information"]["date_of_birth"] == "17/08/1996"
    assert data["personal_information"]["gender"] == "Male"
    assert data["member_information"]["address"] == "44, MG Road, Ernakulam"
    assert data["member_information"]["state"] == "Kerala"
    assert data["member_information"]["pin_code"] == "682001"
    assert data["member_information"]["country"] == "India"

    u.refresh_from_db()
    assert u.full_name == "After Name"
    assert u.email == "after@test.dev"
    profile = MemberComplianceProfile.objects.get(user=u)
    assert profile.date_of_birth.isoformat() == "1996-08-17"
    assert profile.gender == MemberComplianceProfile.Gender.M
    assert profile.full_address == "44, MG Road, Ernakulam"
    assert profile.city == "Kochi"
    assert profile.pin_code == "682001"
    assert profile.state == "Kerala"
    assert profile.country == "India"


@pytest.mark.django_db
def test_me_patch_rejects_duplicate_email():
    taken = User.objects.create_user(
        login_identifier="+919444444446",
        password="pw",
        phone="+919444444446",
        email="taken@test.dev",
        full_name="Taken User",
        member_id="MBR000303",
        referral_code="MBR303",
        referral_link="http://localhost:3000/join?ref=MBR303",
        role=User.Role.MEMBER,
        is_staff=False,
    )
    user = User.objects.create_user(
        login_identifier="+919444444447",
        password="pw",
        phone="+919444444447",
        email="owner@test.dev",
        full_name="Owner User",
        member_id="MBR000304",
        referral_code="MBR304",
        referral_link="http://localhost:3000/join?ref=MBR304",
        role=User.Role.MEMBER,
        is_staff=False,
    )
    client = APIClient()
    client.force_authenticate(user=user)
    resp = client.patch("/api/v1/auth/me/", {"email": taken.email}, format="json")
    assert resp.status_code == 400, resp.content
    assert "email" in (resp.json().get("errors") or {})
