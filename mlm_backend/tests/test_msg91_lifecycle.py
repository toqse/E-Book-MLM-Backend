"""MSG91 lifecycle campaigns: welcome, KYC submitted/approved/rejected."""

from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APIClient

from apps.admin_panel.utils import get_system_config
from apps.agreements.models import LegalDocument, MemberComplianceProfile
from apps.authentication.models import OTPRecord
from apps.notifications.models import NotificationLog
from apps.users.models import User
from tests.conftest import unique_test_aadhaar, unique_test_pan
from tests.test_admin_profile_kyc import _compliance_payload, _latest_admin_kyc_otp, _super_admin
from tests.test_compliance_agreements import _attach_min_kyc_docs, _member_user, _paid_order_for_kyc

REGISTER_PHONE = "+919111222333"
REGISTER_EMAIL = "lifecycle-register@test.dev"


def _enable_msg91(*, development_mode: bool = False, authkey: str = "test-msg91-key"):
    cfg = get_system_config()
    cfg.development_mode = development_mode
    cfg.msg91_authkey = authkey
    cfg.save(update_fields=["development_mode", "msg91_authkey"])


def _sponsor_user():
    return User.objects.create_superuser(
        "sponsor-lifecycle@test.dev",
        "pw",
        full_name="Sponsor Admin",
        email="sponsor-lifecycle@test.dev",
    )


def _support_staff():
    return User.objects.create_user(
        login_identifier="support-lifecycle@test.dev",
        password="pw",
        email="support-lifecycle@test.dev",
        full_name="Support",
        member_id="SUPLC001",
        referral_code="SUPLC1",
        referral_link="http://localhost/join?ref=SUPLC1",
        role=User.Role.SUPPORT,
        is_staff=True,
    )


def _compliance_legal_doc():
    return LegalDocument.objects.create(
        name="Terms",
        category="legal",
        document_type="terms",
        year=2026,
        description="d",
        content_html="<p>x</p>",
        version="1.0",
        requires_acceptance_for_compliance=True,
        is_active=True,
    )


def _accept_compliance_agreement(client, user, doc):
    r_send = client.post(
        "/api/v1/agreements/send-otp/",
        {
            "document_ids": [doc.id],
            "declaration": "Declaration: I accept all these conditions for lifecycle MSG91 tests.",
        },
        format="json",
    )
    assert r_send.status_code == 200
    otp = r_send.json()["data"].get("otp")
    if not otp:
        otp = OTPRecord.objects.filter(
            phone=user.phone,
            purpose=OTPRecord.Purpose.AGREEMENT,
        ).latest("id").otp_code
    assert otp
    rv = client.post(
        "/api/v1/agreements/verify/",
        {"document_ids": [doc.id], "otp_code": otp},
        format="json",
    )
    assert rv.status_code == 200


def _compliance_submit_payload():
    pdf = SimpleUploadedFile("pan.pdf", b"%PDF", content_type="application/pdf")
    aad_front = SimpleUploadedFile("aad_front.pdf", b"%PDF", content_type="application/pdf")
    aad_back = SimpleUploadedFile("aad_back.pdf", b"%PDF", content_type="application/pdf")
    return {
        "date_of_birth": "15/03/1990",
        "gender": "Male",
        "full_address": "1 Main Rd",
        "city": "Kochi",
        "pin_code": "682001",
        "state": "KL",
        "country": "India",
        "pan_number": unique_test_pan(seq=901),
        "name_on_pan": "Lifecycle User",
        "aadhar_number": unique_test_aadhaar(seq=901),
        "name_on_aadhar": "Lifecycle User",
        "nominee_name": "Nominee X",
        "nominee_relationship": "Sibling",
        "nominee_phone": "+919887766553",
        "nominee_date_of_birth": "01/06/1995",
        "account_holder_name": "Lifecycle User",
        "account_number": "12345678901",
        "bank_name": "HDFC Bank",
        "ifsc": "HDFC0000123",
        "branch": "Ernakulam",
        "account_type": "SAVINGS",
        "payout_preference": "BANK",
        "upi_id": "",
        "pan_document": pdf,
        "aadhar_front": aad_front,
        "aadhar_back": aad_back,
    }


@pytest.mark.django_db(transaction=True)
@patch("apps.notifications.msg91.post_campaign", return_value=True)
def test_welcome_registration_sends_msg91(mock_post, system_config):
    _enable_msg91()
    _sponsor_user()
    client = APIClient()

    send = client.post(
        "/api/v1/auth/register/send-otp/",
        {
            "phone": REGISTER_PHONE,
            "email": REGISTER_EMAIL,
            "full_name": "Lifecycle New User",
            "referral_code": "Admin",
        },
        format="json",
    )
    assert send.status_code == 200
    otp = OTPRecord.objects.filter(
        purpose=OTPRecord.Purpose.REGISTER,
        phone=REGISTER_PHONE,
    ).latest("id").otp_code

    finish = client.post(
        "/api/v1/auth/verify-otp-register/",
        {"phone": REGISTER_PHONE, "otp_code": otp},
        format="json",
    )
    assert finish.status_code == 200, finish.content

    slugs = [call.args[0] for call in mock_post.call_args_list]
    assert "welcome-registration" in slugs
    user = User.objects.get(phone=REGISTER_PHONE)
    assert NotificationLog.objects.filter(
        user=user, template_key="welcome_registration", channel="MSG91"
    ).exists()


@pytest.mark.django_db
@patch("apps.notifications.msg91.post_campaign", return_value=True)
def test_welcome_registration_skipped_in_development_mode(mock_post, system_config):
    _enable_msg91(development_mode=True)
    _sponsor_user()
    client = APIClient()

    send = client.post(
        "/api/v1/auth/register/send-otp/",
        {
            "phone": "+919111222334",
            "email": "dev-skip@test.dev",
            "full_name": "Dev Skip User",
            "referral_code": "Admin",
        },
        format="json",
    )
    assert send.status_code == 200
    otp = OTPRecord.objects.filter(phone="+919111222334", purpose=OTPRecord.Purpose.REGISTER).latest(
        "id"
    ).otp_code
    finish = client.post(
        "/api/v1/auth/verify-otp-register/",
        {"phone": "+919111222334", "otp_code": otp},
        format="json",
    )
    assert finish.status_code == 200
    mock_post.assert_not_called()


@pytest.mark.django_db(transaction=True)
@patch("apps.notifications.msg91.post_campaign", return_value=True)
def test_kyc_submitted_on_compliance_submit(mock_post, system_config, primary_ebook):
    _enable_msg91()
    cfg = get_system_config()
    cfg.trigger_instant_kyc_submission = False
    cfg.save(update_fields=["trigger_instant_kyc_submission"])

    doc = _compliance_legal_doc()
    user = _member_user()
    _paid_order_for_kyc(user, primary_ebook)
    client = APIClient()
    client.force_authenticate(user=user)
    _accept_compliance_agreement(client, user, doc)

    r_sub = client.post(
        "/api/v1/auth/compliance/submit/",
        _compliance_submit_payload(),
        format="multipart",
    )
    assert r_sub.status_code == 200, r_sub.content

    slugs = [call.args[0] for call in mock_post.call_args_list]
    assert "kyc-submitted" in slugs
    assert NotificationLog.objects.filter(
        user=user, template_key="kyc_submitted", channel="MSG91"
    ).exists()


@pytest.mark.django_db(transaction=True)
@patch("apps.notifications.msg91.post_campaign", return_value=True)
def test_kyc_approved_on_admin_compliance_approve(mock_post, system_config):
    _enable_msg91()
    user = _member_user("+919887766590")
    profile = MemberComplianceProfile.objects.create(user=user)
    _attach_min_kyc_docs(profile, seq=902)
    user.kyc_status = User.KYCStatus.PENDING
    user.kyc_submitted_at = timezone.now()
    user.save(update_fields=["kyc_status", "kyc_submitted_at", "updated_at"])

    staff_client = APIClient()
    staff_client.force_authenticate(user=_support_staff())
    ap = staff_client.post(f"/api/v1/admin/users/{user.id}/compliance/approve/")
    assert ap.status_code == 200

    slugs = [call.args[0] for call in mock_post.call_args_list]
    assert "kyc-approved" in slugs
    assert NotificationLog.objects.filter(
        user=user, template_key="kyc_approved", channel="MSG91"
    ).exists()


@pytest.mark.django_db(transaction=True)
@patch("apps.notifications.msg91.post_campaign", return_value=True)
def test_kyc_rejected_on_admin_compliance_reject(mock_post, system_config):
    _enable_msg91()
    user = _member_user("+919887766591")
    user.kyc_status = User.KYCStatus.PENDING
    user.kyc_submitted_at = timezone.now()
    user.save(update_fields=["kyc_status", "kyc_submitted_at", "updated_at"])

    staff_client = APIClient()
    staff_client.force_authenticate(user=_support_staff())
    rej = staff_client.post(
        f"/api/v1/admin/users/{user.id}/compliance/reject/",
        {"reason": "mismatch name"},
        format="json",
    )
    assert rej.status_code == 200

    slugs = [call.args[0] for call in mock_post.call_args_list]
    assert "kyc-rejected" in slugs
    rejected_calls = [call for call in mock_post.call_args_list if call.args[0] == "kyc-rejected"]
    body = rejected_calls[0].args[1]
    variables = body["data"]["sendTo"][0]["variables"]
    assert variables["rejection_reason"]["value"] == "mismatch name"
    assert NotificationLog.objects.filter(
        user=user, template_key="kyc_rejected", channel="MSG91"
    ).exists()


@pytest.mark.django_db(transaction=True)
@patch("apps.notifications.msg91.post_campaign", return_value=True)
def test_super_admin_kyc_submit_sends_approved_only(mock_post, system_config):
    _enable_msg91()
    admin = _super_admin(phone="+919887766592", email="super-lifecycle@test.dev")
    client = APIClient()
    client.force_authenticate(user=admin)

    send = client.post("/api/v1/admin/profile/kyc/send-otp/", {}, format="json")
    assert send.status_code == 200
    payload = _compliance_payload()
    payload["otp_code"] = _latest_admin_kyc_otp(admin)
    submit = client.post("/api/v1/admin/profile/kyc/submit/", payload, format="multipart")
    assert submit.status_code == 200

    slugs = [call.args[0] for call in mock_post.call_args_list]
    assert "kyc-approved" in slugs
    assert "kyc-submitted" not in slugs
    assert not NotificationLog.objects.filter(user=admin, template_key="kyc_submitted").exists()
    assert NotificationLog.objects.filter(
        user=admin, template_key="kyc_approved", channel="MSG91"
    ).exists()
