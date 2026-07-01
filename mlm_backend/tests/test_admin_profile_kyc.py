"""SUPER_ADMIN self-service KYC from Admin Profile (OTP + auto-approve)."""

import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.agreements.models import MemberComplianceProfile
from apps.authentication.models import OTPRecord
from apps.users.models import User
from apps.users.services import allocate_member_identity


def _super_admin(*, phone: str | None = None, email: str | None = None) -> User:
    if not phone and not email:
        phone = f"+919{uuid.uuid4().int % 10**9:09d}"
    ident = phone or email or "super@test.dev"
    return User.objects.create_user(
        login_identifier=ident,
        password="pw",
        phone=phone,
        email=email,
        full_name="Super Admin",
        member_id="SUPADM01",
        referral_code="SUPADM1",
        referral_link="http://localhost/join?ref=SUPADM1",
        role=User.Role.SUPER_ADMIN,
        is_staff=True,
    )


def _finance_user() -> User:
    return User.objects.create_user(
        login_identifier="finance@test.dev",
        password="pw",
        email="finance@test.dev",
        full_name="Finance Admin",
        member_id="FINADM01",
        referral_code="FINADM1",
        referral_link="http://localhost/join?ref=FINADM1",
        role=User.Role.FINANCE,
        is_staff=True,
    )


def _member_with_pan_aadhaar(*, pan: str, aadhaar: str) -> User:
    mid, ref, link = allocate_member_identity()
    u = User(
        phone="+919888877766",
        full_name="Other Member",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        pan_number=pan,
        aadhaar_number=aadhaar,
        is_member=True,
    )
    u.set_unusable_password()
    u.save()
    return u


def _compliance_payload(*, files: bool = True) -> dict:
    payload = {
        "date_of_birth": "15/03/1990",
        "gender": "Male",
        "full_address": "1 Admin Tower",
        "city": "Kochi",
        "pin_code": "682001",
        "state": "Kerala",
        "country": "India",
        "pan_number": "ABCDE1234F",
        "name_on_pan": "Super Admin",
        "aadhar_number": "123412341234",
        "name_on_aadhar": "Super Admin",
        "nominee_name": "Nominee X",
        "nominee_relationship": "Sibling",
        "nominee_phone": "+919887766553",
        "nominee_date_of_birth": "01/06/1995",
        "account_holder_name": "Super Admin",
        "account_number": "12345678901",
        "bank_name": "HDFC Bank",
        "ifsc": "HDFC0000123",
        "branch": "Ernakulam",
        "account_type": "SAVINGS",
        "payout_preference": "BANK",
        "upi_id": "",
    }
    if files:
        payload["pan_document"] = SimpleUploadedFile("pan.pdf", b"%PDF", content_type="application/pdf")
        payload["aadhar_front"] = SimpleUploadedFile(
            "aad_front.pdf", b"%PDF", content_type="application/pdf"
        )
        payload["aadhar_back"] = SimpleUploadedFile(
            "aad_back.pdf", b"%PDF", content_type="application/pdf"
        )
    return payload


def _latest_admin_kyc_otp(user: User) -> str:
    phone = (user.phone or "").strip() or None
    email = (user.email or "").strip().lower() or None
    return OTPRecord.objects.filter(
        phone=phone,
        email=email,
        purpose=OTPRecord.Purpose.ADMIN_KYC,
    ).latest("id").otp_code


@pytest.mark.django_db
def test_super_admin_send_otp_and_submit_verifies_kyc(system_config):
    admin = _super_admin()
    client = APIClient()
    client.force_authenticate(user=admin)

    send = client.post("/api/v1/admin/profile/kyc/send-otp/", {}, format="json")
    assert send.status_code == 200, send.content
    otp = _latest_admin_kyc_otp(admin)

    payload = _compliance_payload()
    payload["otp_code"] = otp
    submit = client.post("/api/v1/admin/profile/kyc/submit/", payload, format="multipart")
    assert submit.status_code == 200, submit.content
    body = submit.json()
    assert body["success"] is True
    assert body["data"]["kyc_status"] == User.KYCStatus.VERIFIED

    admin.refresh_from_db()
    assert admin.kyc_status == User.KYCStatus.VERIFIED
    assert admin.kyc_first_approved_at is not None
    assert admin.kyc_reviewed_at is not None
    assert admin.compliance_submission_version == 1

    profile = MemberComplianceProfile.objects.get(user=admin)
    assert profile.pan_number == "ABCDE1234F"
    assert profile.gender == MemberComplianceProfile.Gender.M
    assert profile.pan_document
    assert profile.aadhar_front
    assert profile.aadhar_back


@pytest.mark.django_db
def test_finance_user_forbidden_on_admin_profile_kyc(system_config):
    finance = _finance_user()
    client = APIClient()
    client.force_authenticate(user=finance)

    send = client.post("/api/v1/admin/profile/kyc/send-otp/", {}, format="json")
    assert send.status_code == 403

    payload = _compliance_payload()
    payload["otp_code"] = "000000"
    submit = client.post("/api/v1/admin/profile/kyc/submit/", payload, format="multipart")
    assert submit.status_code == 403


@pytest.mark.django_db
def test_submit_without_otp_rejected(system_config):
    admin = _super_admin()
    client = APIClient()
    client.force_authenticate(user=admin)

    payload = _compliance_payload()
    submit = client.post("/api/v1/admin/profile/kyc/submit/", payload, format="multipart")
    assert submit.status_code == 400
    assert submit.json()["success"] is False


@pytest.mark.django_db
def test_submit_with_wrong_otp_rejected(system_config):
    admin = _super_admin()
    client = APIClient()
    client.force_authenticate(user=admin)

    client.post("/api/v1/admin/profile/kyc/send-otp/", {}, format="json")

    payload = _compliance_payload()
    payload["otp_code"] = "000000"
    submit = client.post("/api/v1/admin/profile/kyc/submit/", payload, format="multipart")
    assert submit.status_code == 400
    assert submit.json()["success"] is False


@pytest.mark.django_db
def test_first_submit_requires_document_files(system_config):
    admin = _super_admin()
    client = APIClient()
    client.force_authenticate(user=admin)
    client.post("/api/v1/admin/profile/kyc/send-otp/", {}, format="json")
    otp = _latest_admin_kyc_otp(admin)

    payload = _compliance_payload(files=False)
    payload["otp_code"] = otp
    submit = client.post("/api/v1/admin/profile/kyc/submit/", payload, format="multipart")
    assert submit.status_code == 400
    msg = submit.json()["message"].lower()
    assert "required" in msg


@pytest.mark.django_db
def test_duplicate_pan_rejected(system_config):
    admin = _super_admin()
    _member_with_pan_aadhaar(pan="ABCDE1234F", aadhaar="999988887777")
    client = APIClient()
    client.force_authenticate(user=admin)
    client.post("/api/v1/admin/profile/kyc/send-otp/", {}, format="json")
    otp = _latest_admin_kyc_otp(admin)

    payload = _compliance_payload()
    payload["otp_code"] = otp
    submit = client.post("/api/v1/admin/profile/kyc/submit/", payload, format="multipart")
    assert submit.status_code == 400
    assert submit.json()["success"] is False


@pytest.mark.django_db
def test_resubmit_allows_existing_files_without_reupload(system_config):
    admin = _super_admin()
    client = APIClient()
    client.force_authenticate(user=admin)

    client.post("/api/v1/admin/profile/kyc/send-otp/", {}, format="json")
    payload = _compliance_payload()
    payload["otp_code"] = _latest_admin_kyc_otp(admin)
    first = client.post("/api/v1/admin/profile/kyc/submit/", payload, format="multipart")
    assert first.status_code == 200, first.content

    client.post("/api/v1/admin/profile/kyc/send-otp/", {}, format="json")
    resubmit = _compliance_payload(files=False)
    resubmit["otp_code"] = _latest_admin_kyc_otp(admin)
    resubmit["full_address"] = "2 Admin Tower Updated"
    second = client.post("/api/v1/admin/profile/kyc/submit/", resubmit, format="multipart")
    assert second.status_code == 200, second.content

    admin.refresh_from_db()
    assert admin.kyc_status == User.KYCStatus.VERIFIED
    profile = MemberComplianceProfile.objects.get(user=admin)
    assert profile.full_address == "2 Admin Tower Updated"
    assert profile.pan_document
    assert admin.compliance_submission_version == 2
