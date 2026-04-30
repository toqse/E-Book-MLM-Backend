import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.agreements.models import LegalDocument, MemberComplianceProfile, UserAgreementAcceptance
from apps.users.models import User
from apps.users.services import allocate_member_identity


def _member_user(phone: str = "+919887766554") -> User:
    mid, rc, lk = allocate_member_identity()
    u = User(
        login_identifier=phone,
        phone=phone,
        full_name="Compliance Test",
        member_id=mid,
        referral_code=rc,
        referral_link=lk,
    )
    u.set_unusable_password()
    u.save()
    return u


@pytest.mark.django_db
def test_kyc_submit_and_bank_deprecated_410():
    u = _member_user()
    client = APIClient()
    client.force_authenticate(user=u)
    r = client.post("/api/v1/auth/kyc/submit/", {"pan": "X"}, format="json")
    assert r.status_code == 410
    r2 = client.post("/api/v1/auth/bank/", {}, format="json")
    assert r2.status_code == 410


@pytest.mark.django_db
def test_compliance_flow_with_agreement_otp_and_admin_approve():
    LegalDocument.objects.create(
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
    doc = LegalDocument.objects.get()

    u = _member_user()
    staff = User.objects.create_user(
        login_identifier="support@test.dev",
        password="pw",
        email="support@test.dev",
        full_name="Support",
        member_id="SUP000001",
        referral_code="SUP001",
        referral_link="http://localhost/join?ref=SUP001",
        role=User.Role.SUPPORT,
        is_staff=True,
    )

    client = APIClient()
    client.force_authenticate(user=u)

    r0 = client.post(
        "/api/v1/auth/compliance/submit/",
        {"dummy": "1"},
        format="multipart",
    )
    assert r0.status_code == 400

    r_send = client.post(
        "/api/v1/agreements/send-otp/",
        {"document_ids": [doc.id]},
        format="json",
    )
    assert r_send.status_code == 200
    otp = r_send.json()["data"].get("otp")
    assert otp

    rv = client.post(
        "/api/v1/agreements/verify/",
        {"document_ids": [doc.id], "otp_code": otp},
        format="json",
    )
    assert rv.status_code == 200
    assert UserAgreementAcceptance.objects.filter(user=u, document=doc).exists()

    pdf = SimpleUploadedFile("pan.pdf", b"%PDF", content_type="application/pdf")
    aad = SimpleUploadedFile("aad.pdf", b"%PDF", content_type="application/pdf")

    payload = {
        "date_of_birth": "15/03/1990",
        "gender": "Male",
        "full_address": "1 Main Rd",
        "city": "Kochi",
        "pin_code": "682001",
        "state": "KL",
        "country": "India",
        "pan_number": "ABCDE1234F",
        "name_on_pan": "Test User",
        "aadhar_number": "123412341234",
        "name_on_aadhar": "Test User",
        "nominee_name": "Nominee X",
        "nominee_relationship": "Sibling",
        "nominee_phone": "+919887766553",
        "nominee_date_of_birth": "01/06/1995",
        "account_holder_name": "Test User",
        "account_number": "12345678901",
        "bank_name": "HDFC Bank",
        "ifsc": "HDFC0000123",
        "branch": "Ernakulam",
        "account_type": "SAVINGS",
        "payout_preference": "BANK",
        "upi_id": "",
        "pan_document": pdf,
        "aadhar_document": aad,
    }
    r_sub = client.post("/api/v1/auth/compliance/submit/", payload, format="multipart")
    assert r_sub.status_code == 200, r_sub.content
    u.refresh_from_db()
    assert u.kyc_status == User.KYCStatus.PENDING
    profile = MemberComplianceProfile.objects.get(user=u)
    assert profile.gender == "M"

    staff_client = APIClient()
    staff_client.force_authenticate(user=staff)
    q = staff_client.get("/api/v1/admin/compliance-queue/")
    assert q.status_code == 200
    assert any(row["user_id"] == u.id for row in q.json()["data"]["results"])

    ap = staff_client.post(f"/api/v1/admin/users/{u.id}/compliance/approve/")
    assert ap.status_code == 200
    u.refresh_from_db()
    assert u.kyc_status == User.KYCStatus.VERIFIED


@pytest.mark.django_db
def test_admin_agreement_crud_superadmin():
    su = User.objects.create_superuser(
        "admin@test.dev",
        "pw",
        full_name="Admin",
        email="admin@test.dev",
    )
    client = APIClient()
    client.force_authenticate(user=su)
    r = client.post(
        "/api/v1/admin/agreements/",
        {
            "name": "Policy",
            "category": "c",
            "document_type": "t",
            "year": 2026,
            "description": "d",
            "content_html": "<p>a</p>",
            "version": "1.0",
            "is_active": True,
            "requires_acceptance_for_compliance": False,
        },
        format="json",
    )
    assert r.status_code == 201
    pid = r.json()["data"]["id"]
    g = client.get("/api/v1/admin/agreements/")
    assert g.status_code == 200
    assert len(g.json()["data"]["results"]) >= 1
    d = client.delete(f"/api/v1/admin/agreements/{pid}/")
    assert d.status_code == 200
    assert not LegalDocument.objects.get(pk=pid).is_active
