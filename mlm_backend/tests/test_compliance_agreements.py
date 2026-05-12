import io
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.agreements.models import (
    LegalDocument,
    MemberComplianceProfile,
    UserAgreementAcceptance,
    UserAgreementAcceptanceDeclaration,
)
from apps.agreements.proof_service import verify_hmac_for_batch
from apps.courses.models import EBook
from apps.payments.models import Order
from apps.payments.services import finalize_order_as_paid
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


def _attach_min_kyc_docs(profile: MemberComplianceProfile):
    profile.pan_number = "ABCDE1234F"
    profile.aadhar_number = "123412341234"
    profile.pan_document = SimpleUploadedFile("pan.pdf", b"fake", content_type="application/pdf")
    profile.aadhar_front = SimpleUploadedFile(
        "aad_front.pdf", b"fake", content_type="application/pdf"
    )
    profile.aadhar_back = SimpleUploadedFile(
        "aad_back.pdf", b"fake", content_type="application/pdf"
    )
    profile.save()


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
        {
            "document_ids": [doc.id],
            "declaration": "Declaration: I accept all these conditions for compliance testing.",
        },
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
    aad_front = SimpleUploadedFile("aad_front.pdf", b"%PDF", content_type="application/pdf")
    aad_back = SimpleUploadedFile("aad_back.pdf", b"%PDF", content_type="application/pdf")

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
        "aadhar_front": aad_front,
        "aadhar_back": aad_back,
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
    queue_rows = q.json()["data"]["results"]
    target_row = next(row for row in queue_rows if row["user_id"] == u.id)
    assert target_row["aadhar_front_url"]
    assert target_row["aadhar_back_url"]

    ap = staff_client.post(f"/api/v1/admin/users/{u.id}/compliance/approve/")
    assert ap.status_code == 200
    u.refresh_from_db()
    assert u.kyc_status == User.KYCStatus.VERIFIED


@pytest.mark.django_db
def test_admin_bulk_compliance_approve_accepts_body_ids():
    u1 = _member_user("+919887766560")
    u2 = _member_user("+919887766561")
    staff = User.objects.create_user(
        login_identifier="support2@test.dev",
        password="pw",
        email="support2@test.dev",
        full_name="Support 2",
        member_id="SUP000002",
        referral_code="SUP002",
        referral_link="http://localhost/join?ref=SUP002",
        role=User.Role.SUPPORT,
        is_staff=True,
    )
    p1 = MemberComplianceProfile.objects.create(user=u1)
    p2 = MemberComplianceProfile.objects.create(user=u2)
    _attach_min_kyc_docs(p1)
    _attach_min_kyc_docs(p2)

    staff_client = APIClient()
    staff_client.force_authenticate(user=staff)

    r = staff_client.post(
        "/api/v1/admin/users/compliance/approve/",
        {"user_ids": [u1.id, u2.id]},
        format="json",
    )
    assert r.status_code == 200, r.content
    body = r.json()["data"]
    assert set(body["approved_ids"]) == {u1.id, u2.id}

    u1.refresh_from_db()
    u2.refresh_from_db()
    assert u1.kyc_status == User.KYCStatus.VERIFIED
    assert u2.kyc_status == User.KYCStatus.VERIFIED


@pytest.mark.django_db
def test_admin_bulk_compliance_approve_partial_failures_sets_message_and_errors():
    u1 = _member_user("+919887766570")
    u2 = _member_user("+919887766571")
    staff = User.objects.create_user(
        login_identifier="support3@test.dev",
        password="pw",
        email="support3@test.dev",
        full_name="Support 3",
        member_id="SUP000003",
        referral_code="SUP003",
        referral_link="http://localhost/join?ref=SUP003",
        role=User.Role.SUPPORT,
        is_staff=True,
    )
    p1 = MemberComplianceProfile.objects.create(user=u1)
    _attach_min_kyc_docs(p1)
    # u2 intentionally has no compliance profile -> should fail

    staff_client = APIClient()
    staff_client.force_authenticate(user=staff)
    r = staff_client.post(
        "/api/v1/admin/users/compliance/approve/",
        {"user_ids": [u1.id, u2.id]},
        format="json",
    )
    assert r.status_code == 200, r.content
    j = r.json()
    assert j["success"] is False
    assert j["message"] == "Approved with some failures"
    assert j["errors"]["detail"] == "Approved with some failures"
    assert set(j["data"]["approved_ids"]) == {u1.id}
    assert any(x["id"] == u2.id for x in j["data"]["failed"])


@pytest.mark.django_db
def test_admin_bulk_compliance_approve_all_failures_returns_400():
    u1 = _member_user("+919887766580")
    staff = User.objects.create_user(
        login_identifier="support4@test.dev",
        password="pw",
        email="support4@test.dev",
        full_name="Support 4",
        member_id="SUP000004",
        referral_code="SUP004",
        referral_link="http://localhost/join?ref=SUP004",
        role=User.Role.SUPPORT,
        is_staff=True,
    )
    # u1 has no compliance profile -> should fail
    staff_client = APIClient()
    staff_client.force_authenticate(user=staff)
    r = staff_client.post(
        "/api/v1/admin/users/compliance/approve/",
        {"user_ids": [u1.id]},
        format="json",
    )
    assert r.status_code == 400, r.content
    j = r.json()
    assert j["success"] is False
    assert j["message"] == "Member has no compliance profile to approve."
    assert j["errors"]["detail"] == "Member has no compliance profile to approve."


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
            "category": "LEGAL DOCUMENT",
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


@pytest.mark.django_db
def test_admin_agreements_list_supports_category_filter_and_normalization():
    su = User.objects.create_superuser(
        "admin2@test.dev",
        "pw",
        full_name="Admin 2",
        email="admin2@test.dev",
    )
    client = APIClient()
    client.force_authenticate(user=su)

    LegalDocument.objects.create(
        name="KYC Policy",
        category="KYC & IDENTITY",
        document_type="policy",
        year=2026,
        description="d",
        content_html="<p>a</p>",
        version="1.0",
        is_active=True,
        requires_acceptance_for_compliance=False,
    )
    LegalDocument.objects.create(
        name="Legal Policy",
        category="LEGAL DOCUMENT",
        document_type="policy",
        year=2026,
        description="d",
        content_html="<p>b</p>",
        version="1.0",
        is_active=True,
        requires_acceptance_for_compliance=False,
    )

    filtered = client.get("/api/v1/admin/agreements/", {"category": "KYC and IDENTITY"})
    assert filtered.status_code == 200
    rows = filtered.json()["data"]["results"]
    assert len(rows) == 1
    assert rows[0]["category"] == "KYC & IDENTITY"


@pytest.mark.django_db
def test_agreements_list_supports_category_filter():
    u = _member_user("+919887766500")
    client = APIClient()
    client.force_authenticate(user=u)
    LegalDocument.objects.create(
        name="Terms",
        category="Legal",
        document_type="terms",
        year=2026,
        description="d",
        content_html="<p>x</p>",
        version="1.0",
        requires_acceptance_for_compliance=True,
        is_active=True,
    )
    LegalDocument.objects.create(
        name="Refund",
        category="Payments",
        document_type="policy",
        year=2026,
        description="d",
        content_html="<p>y</p>",
        version="1.0",
        requires_acceptance_for_compliance=False,
        is_active=True,
    )

    all_resp = client.get("/api/v1/agreements/")
    assert all_resp.status_code == 200
    body = all_resp.json()["data"]
    assert len(body["results"]) == 2
    assert "user" in body
    assert len(body["user"]) == 1
    assert body["user"][0]["id"] == u.id
    assert body["user"][0]["order_invoices"] == []
    assert body["user"][0]["compliance_acceptance_proof"] is None

    filtered = client.get("/api/v1/agreements/", {"category": "legal"})
    assert filtered.status_code == 200
    rows = filtered.json()["data"]["results"]
    assert len(rows) == 1
    assert rows[0]["category"] == "Legal"


@pytest.mark.django_db
def test_agreements_compliance_legal_list_filters():
    u = _member_user("+919887766501")
    client = APIClient()
    client.force_authenticate(user=u)
    LegalDocument.objects.create(
        name="Legal Req",
        category="LEGAL DOCUMENT",
        document_type="terms",
        year=2026,
        description="d",
        content_html="<p>a</p>",
        version="1.0",
        requires_acceptance_for_compliance=True,
        is_active=True,
    )
    LegalDocument.objects.create(
        name="Legal Optional",
        category="legal document",
        document_type="terms",
        year=2026,
        description="d",
        content_html="<p>b</p>",
        version="1.0",
        requires_acceptance_for_compliance=False,
        is_active=True,
    )
    LegalDocument.objects.create(
        name="KYC Req",
        category="KYC & IDENTITY",
        document_type="id",
        year=2026,
        description="d",
        content_html="<p>c</p>",
        version="1.0",
        requires_acceptance_for_compliance=True,
        is_active=True,
    )
    r = client.get("/api/v1/agreements/compliance-legal/")
    assert r.status_code == 200
    rows = r.json()["data"]["results"]
    assert len(rows) == 1
    assert rows[0]["name"] == "Legal Req"
    assert rows[0]["requires_acceptance_for_compliance"] is True
    assert rows[0]["is_agreement_accepted"] is False


@pytest.mark.django_db
def test_agreements_get_user_invoices_acceptance_proof_and_download(system_config):
    LegalDocument.objects.create(
        name="Terms Proof",
        category="LEGAL DOCUMENT",
        document_type="terms",
        year=2026,
        description="d",
        content_html="<p>x</p>",
        version="1.0",
        requires_acceptance_for_compliance=True,
        is_active=True,
    )
    doc = LegalDocument.objects.get(name="Terms Proof")
    decl_text = "Declaration: I accept all these conditions for proof PDF testing."

    u = _member_user("+919887766502")
    client = APIClient()
    client.force_authenticate(user=u)

    r_send = client.post(
        "/api/v1/agreements/send-otp/",
        {"document_ids": [doc.id], "declaration": decl_text},
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
    batch_id = rv.json()["data"]["acceptance_batch_id"]
    decl_row = UserAgreementAcceptanceDeclaration.objects.get(user=u, acceptance_batch_id=batch_id)
    assert decl_row.declaration_text == decl_text.strip()

    book = EBook.objects.create(
        title="Proof Book",
        slug="proof-book",
        category="X",
        description="d",
        pages_count=10,
        language="English",
        price=200,
        status=EBook.Status.PUBLISHED,
        file_url="https://example.com/b.pdf",
        is_active=True,
    )
    order = Order.objects.create(
        user=u,
        ebook=book,
        order_number="ORD-PROOF-AG-1",
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("241.72"),
        status=Order.Status.CREATED,
        razorpay_order_id="ord_proof_ag",
    )
    finalize_order_as_paid(order, payment_id="pay_proof_ag")

    r_list = client.get("/api/v1/agreements/")
    assert r_list.status_code == 200
    payload = r_list.json()["data"]
    assert "user" in payload
    assert len(payload["user"]) == 1
    row = payload["user"][0]
    assert row["id"] == u.id
    assert len(row["order_invoices"]) == 1
    inv_row = row["order_invoices"][0]
    assert inv_row["order_id"] == order.id
    assert inv_row["invoice_number"]
    assert inv_row["invoice_pdf_url"]

    proof_meta = row["compliance_acceptance_proof"]
    assert proof_meta is not None
    assert proof_meta["acceptance_batch_id"] == batch_id
    assert proof_meta["pdf_download_url"]
    assert proof_meta["verification"]["algo"] == "HMAC-SHA256"
    sig = proof_meta["verification"]["signature"]
    assert verify_hmac_for_batch(
        user_id=u.id,
        acceptance_batch_id=batch_id,
        signature_hex=sig,
    )

    dl = client.get(
        f"/api/v1/agreements/acceptance-proof/{batch_id}/download/",
    )
    assert dl.status_code == 200
    assert dl["Content-Type"].startswith("application/pdf")
    assert "attachment" in (dl.get("Content-Disposition") or "").lower()
    pdf_bytes = b"".join(dl.streaming_content)
    assert pdf_bytes[:4] == b"%PDF"
    # Declaration is bound in HMAC v2 and stored on UserAgreementAcceptanceDeclaration;
    # embedded PDF text may be compressed so we do not assert raw substring here.

    # Browser-style GET: same URL with signed token, no Authorization header
    from urllib.parse import parse_qs, urlparse

    anon = APIClient()
    parsed = urlparse(proof_meta["pdf_download_url"])
    qs = parse_qs(parsed.query)
    assert "token" in qs and qs["token"][0]
    path_qs = f"{parsed.path}?{parsed.query}"
    dl2 = anon.get(path_qs)
    assert dl2.status_code == 200
    assert b"".join(dl2.streaming_content)[:4] == b"%PDF"


@pytest.mark.django_db
def test_admin_agreement_rejects_invalid_category():
    su = User.objects.create_superuser(
        "admin2@test.dev",
        "pw",
        full_name="Admin",
        email="admin2@test.dev",
    )
    client = APIClient()
    client.force_authenticate(user=su)
    r = client.post(
        "/api/v1/admin/agreements/",
        {
            "name": "Bad",
            "category": "Other",
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
    assert r.status_code == 400
