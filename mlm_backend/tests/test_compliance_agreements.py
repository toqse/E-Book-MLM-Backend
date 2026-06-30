import io
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APIClient

from apps.admin_panel.utils import get_system_config
from apps.agreements.models import (
    AgreementCategory,
    LegalDocument,
    MemberComplianceProfile,
    UserAgreementAcceptance,
    UserAgreementAcceptanceDeclaration,
)
from apps.agreements.proof_pdf import acceptance_proof_pdf_page_count
from apps.agreements.proof_service import verify_hmac_for_batch
from apps.courses.models import EBook
from apps.payments.models import Order
from apps.payments.services import finalize_order_as_paid
from apps.users.models import User
from apps.users.services import allocate_member_identity
from tests.conftest import unique_test_aadhaar, unique_test_pan


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


def _paid_order_for_kyc(user: User, ebook: EBook) -> Order:
    order = Order.objects.create(
        user=user,
        ebook=ebook,
        order_number=f"ORD-KYC-AG-{user.id}",
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("241.72"),
        status=Order.Status.CREATED,
        razorpay_order_id="ord_kyc_ag",
    )
    finalize_order_as_paid(order, payment_id="pay_kyc_ag")
    order.refresh_from_db()
    order.refund_eligible_until = timezone.now() - timedelta(days=1)
    order.save(update_fields=["refund_eligible_until"])
    return order


def _attach_min_kyc_docs(profile: MemberComplianceProfile, *, seq: int | None = None):
    profile.pan_number = unique_test_pan(seq=seq)
    profile.aadhar_number = unique_test_aadhaar(seq=seq)
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
def test_compliance_flow_with_agreement_otp_and_admin_approve(system_config, primary_ebook):
    cfg = get_system_config()
    cfg.trigger_instant_kyc_submission = False
    cfg.save(update_fields=["trigger_instant_kyc_submission"])

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
    _paid_order_for_kyc(u, primary_ebook)
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
    _attach_min_kyc_docs(p1, seq=1)
    _attach_min_kyc_docs(p2, seq=2)

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
        name="KYC Doc",
        category="KYC & IDENTITY",
        document_type="id",
        year=2026,
        description="d",
        content_html="<p>c</p>",
        version="1.0",
        requires_acceptance_for_compliance=False,
        is_active=True,
    )
    r = client.get("/api/v1/agreements/compliance-legal/")
    assert r.status_code == 200
    data = r.json()["data"]
    rows = data["results"]
    assert data["user"] == {"id": u.id, "full_name": u.full_name}
    assert len(rows) == 1
    assert rows[0]["name"] == "Legal Req"
    assert rows[0]["requires_acceptance_for_compliance"] is True
    assert rows[0]["is_agreement_accepted"] is False


@pytest.mark.django_db
def test_agreements_get_user_invoices_acceptance_proof_and_download(
    system_config, primary_ebook
):
    cfg = get_system_config()
    cfg.trigger_instant_kyc_submission = False
    cfg.save(update_fields=["trigger_instant_kyc_submission"])

    LegalDocument.objects.create(
        name="Terms Proof",
        category=AgreementCategory.KYC_IDENTITY,
        document_type="terms",
        year=2026,
        description="d",
        content_html="<p>KYC agreement body for PDF appendix.</p>",
        version="1.0",
        requires_acceptance_for_compliance=True,
        is_active=True,
    )
    doc = LegalDocument.objects.get(name="Terms Proof")
    decl_text = "Declaration: I accept all these conditions for proof PDF testing."

    u = _member_user("+919887766502")
    _paid_order_for_kyc(u, primary_ebook)
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
    assert len(row["order_invoices"]) == 2
    inv_row = next(r for r in row["order_invoices"] if r["order_id"] == order.id)
    assert inv_row is not None
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
    assert acceptance_proof_pdf_page_count(pdf_bytes) >= 2
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
def test_admin_compliance_required_singleton_upsert_and_member_lists():
    su = User.objects.create_superuser(
        "admin-singleton@test.dev",
        "pw",
        full_name="Admin Singleton",
        email="admin-singleton@test.dev",
    )
    admin = APIClient()
    admin.force_authenticate(user=su)

    old = LegalDocument.objects.create(
        name="Old Compliance",
        category=AgreementCategory.LEGAL_DOCUMENT,
        document_type="terms",
        year=2025,
        description="old",
        content_html="<p>old</p>",
        version="0.9",
        requires_acceptance_for_compliance=True,
        is_active=True,
    )
    other = LegalDocument.objects.create(
        name="Other Legal",
        category=AgreementCategory.LEGAL_DOCUMENT,
        document_type="policy",
        year=2026,
        description="other",
        content_html="<p>other</p>",
        version="1.0",
        requires_acceptance_for_compliance=False,
        is_active=True,
    )

    g0 = admin.get("/api/v1/admin/agreements/compliance-required/")
    assert g0.status_code == 200
    assert g0.json()["data"]["id"] == old.id

    r1 = admin.post(
        "/api/v1/admin/agreements/compliance-required/",
        {
            "name": "Compliance Terms v2",
            "document_type": "terms",
            "year": 2026,
            "description": "updated singleton",
            "content_html": "<p>v2</p>",
            "version": "2.0",
            "is_active": True,
        },
        format="json",
    )
    assert r1.status_code == 200, r1.content
    body1 = r1.json()["data"]
    assert body1["id"] == old.id
    assert body1["name"] == "Compliance Terms v2"
    assert body1["version"] == "2.0"
    assert body1["category"] == AgreementCategory.LEGAL_DOCUMENT
    assert body1["requires_acceptance_for_compliance"] is True

    old.refresh_from_db()
    assert old.name == "Compliance Terms v2"
    assert LegalDocument.objects.filter(requires_acceptance_for_compliance=True).count() == 1

    r2 = admin.post(
        "/api/v1/admin/agreements/compliance-required/",
        {
            "name": "Compliance Terms v3",
            "document_type": "terms",
            "year": 2026,
            "description": "rewritten again",
            "content_html": "<p>v3</p>",
            "version": "3.0",
            "is_active": True,
        },
        format="json",
    )
    assert r2.status_code == 200
    assert r2.json()["data"]["id"] == old.id
    assert r2.json()["data"]["version"] == "3.0"
    assert LegalDocument.objects.count() == 2

    member = _member_user("+919887766599")
    mclient = APIClient()
    mclient.force_authenticate(user=member)

    compliance = mclient.get("/api/v1/agreements/compliance-legal/")
    assert compliance.status_code == 200
    rows = compliance.json()["data"]["results"]
    assert len(rows) == 1
    assert rows[0]["id"] == old.id
    assert rows[0]["name"] == "Compliance Terms v3"

    all_docs = mclient.get("/api/v1/agreements/")
    assert all_docs.status_code == 200
    result_ids = {r["id"] for r in all_docs.json()["data"]["results"]}
    assert old.id in result_ids
    assert other.id in result_ids

    d = admin.delete("/api/v1/admin/agreements/compliance-required/")
    assert d.status_code == 200
    old.refresh_from_db()
    assert old.is_active is False
    assert old.requires_acceptance_for_compliance is False

    empty = mclient.get("/api/v1/agreements/compliance-legal/")
    assert empty.status_code == 200
    assert empty.json()["data"]["results"] == []


@pytest.mark.django_db
def test_admin_compliance_required_singleton_create_clears_previous_flag():
    su = User.objects.create_superuser(
        "admin-singleton2@test.dev",
        "pw",
        full_name="Admin Singleton 2",
        email="admin-singleton2@test.dev",
    )
    admin = APIClient()
    admin.force_authenticate(user=su)

    stale = LegalDocument.objects.create(
        name="Stale",
        category="LEGAL DOCUMENT",
        document_type="terms",
        year=2024,
        description="d",
        content_html="<p>s</p>",
        version="1.0",
        requires_acceptance_for_compliance=True,
        is_active=True,
    )
    admin.delete("/api/v1/admin/agreements/compliance-required/")
    stale.refresh_from_db()
    assert stale.requires_acceptance_for_compliance is False

    created = admin.post(
        "/api/v1/admin/agreements/compliance-required/",
        {
            "name": "Fresh Compliance",
            "document_type": "terms",
            "year": 2026,
            "description": "new",
            "content_html": "<p>n</p>",
            "version": "1.0",
            "is_active": True,
        },
        format="json",
    )
    assert created.status_code == 201
    new_id = created.json()["data"]["id"]
    stale.refresh_from_db()
    assert stale.requires_acceptance_for_compliance is False
    assert LegalDocument.objects.filter(requires_acceptance_for_compliance=True).count() == 1
    assert LegalDocument.objects.get(pk=new_id).requires_acceptance_for_compliance is True


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


def _compliance_legal_doc() -> LegalDocument:
    doc, _ = LegalDocument.objects.get_or_create(
        name="Terms Uniq",
        defaults={
            "category": "legal",
            "document_type": "terms",
            "year": 2026,
            "description": "d",
            "content_html": "<p>x</p>",
            "version": "1.0",
            "requires_acceptance_for_compliance": True,
            "is_active": True,
        },
    )
    doc.requires_acceptance_for_compliance = True
    doc.is_active = True
    doc.save()
    return doc


def _accept_compliance_agreements(client: APIClient, user: User, doc: LegalDocument) -> None:
    r_send = client.post(
        "/api/v1/agreements/send-otp/",
        {
            "document_ids": [doc.id],
            "declaration": "I accept for uniqueness testing.",
        },
        format="json",
    )
    assert r_send.status_code == 200, r_send.content
    otp = r_send.json()["data"].get("otp")
    rv = client.post(
        "/api/v1/agreements/verify/",
        {"document_ids": [doc.id], "otp_code": otp},
        format="json",
    )
    assert rv.status_code == 200, rv.content


def _compliance_submit_payload(**overrides):
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
        "pan_document": SimpleUploadedFile("pan.pdf", b"%PDF", content_type="application/pdf"),
        "aadhar_front": SimpleUploadedFile(
            "aad_front.pdf", b"%PDF", content_type="application/pdf"
        ),
        "aadhar_back": SimpleUploadedFile(
            "aad_back.pdf", b"%PDF", content_type="application/pdf"
        ),
    }
    payload.update(overrides)
    return payload


@pytest.mark.django_db
def test_compliance_submit_rejects_duplicate_pan(system_config, primary_ebook):
    cfg = get_system_config()
    cfg.trigger_instant_kyc_submission = True
    cfg.save(update_fields=["trigger_instant_kyc_submission"])
    doc = _compliance_legal_doc()

    u1 = _member_user("+919887766601")
    u2 = _member_user("+919887766602")
    _paid_order_for_kyc(u1, primary_ebook)
    _paid_order_for_kyc(u2, primary_ebook)

    c1 = APIClient()
    c1.force_authenticate(user=u1)
    _accept_compliance_agreements(c1, u1, doc)
    r1 = c1.post(
        "/api/v1/auth/compliance/submit/",
        _compliance_submit_payload(),
        format="multipart",
    )
    assert r1.status_code == 200, r1.content

    c2 = APIClient()
    c2.force_authenticate(user=u2)
    _accept_compliance_agreements(c2, u2, doc)
    r2 = c2.post(
        "/api/v1/auth/compliance/submit/",
        _compliance_submit_payload(pan_number="ABCDE1234F", aadhar_number="999988887777"),
        format="multipart",
    )
    assert r2.status_code == 400
    body = r2.json()
    assert "pan_number" in (body.get("errors") or {})


@pytest.mark.django_db
def test_compliance_submit_rejects_duplicate_aadhaar(system_config, primary_ebook):
    cfg = get_system_config()
    cfg.trigger_instant_kyc_submission = True
    cfg.save(update_fields=["trigger_instant_kyc_submission"])
    doc = _compliance_legal_doc()

    u1 = _member_user("+919887766611")
    u2 = _member_user("+919887766612")
    _paid_order_for_kyc(u1, primary_ebook)
    _paid_order_for_kyc(u2, primary_ebook)

    c1 = APIClient()
    c1.force_authenticate(user=u1)
    _accept_compliance_agreements(c1, u1, doc)
    assert (
        c1.post(
            "/api/v1/auth/compliance/submit/",
            _compliance_submit_payload(),
            format="multipart",
        ).status_code
        == 200
    )

    c2 = APIClient()
    c2.force_authenticate(user=u2)
    _accept_compliance_agreements(c2, u2, doc)
    r2 = c2.post(
        "/api/v1/auth/compliance/submit/",
        _compliance_submit_payload(pan_number="ABCDE9999Z", aadhar_number="123412341234"),
        format="multipart",
    )
    assert r2.status_code == 400
    body = r2.json()
    assert "aadhar_number" in (body.get("errors") or {})


@pytest.mark.django_db
def test_compliance_submit_allows_same_user_resubmit(system_config, primary_ebook):
    cfg = get_system_config()
    cfg.trigger_instant_kyc_submission = True
    cfg.save(update_fields=["trigger_instant_kyc_submission"])
    doc = _compliance_legal_doc()

    u = _member_user("+919887766621")
    _paid_order_for_kyc(u, primary_ebook)
    client = APIClient()
    client.force_authenticate(user=u)
    _accept_compliance_agreements(client, u, doc)
    payload = _compliance_submit_payload()
    assert client.post("/api/v1/auth/compliance/submit/", payload, format="multipart").status_code == 200
    u.refresh_from_db()
    assert u.aadhaar_number == "123412341234"
    r2 = client.post("/api/v1/auth/compliance/submit/", payload, format="multipart")
    assert r2.status_code == 200, r2.content


@pytest.mark.django_db
def test_compliance_submit_without_pan_succeeds(system_config, primary_ebook):
    cfg = get_system_config()
    cfg.trigger_instant_kyc_submission = True
    cfg.save(update_fields=["trigger_instant_kyc_submission"])
    doc = _compliance_legal_doc()

    u = _member_user("+919887766631")
    _paid_order_for_kyc(u, primary_ebook)
    client = APIClient()
    client.force_authenticate(user=u)
    _accept_compliance_agreements(client, u, doc)

    payload = _compliance_submit_payload(
        pan_number="",
        name_on_pan="",
        aadhar_number=unique_test_aadhaar(),
    )
    payload.pop("pan_document", None)

    r = client.post("/api/v1/auth/compliance/submit/", payload, format="multipart")
    assert r.status_code == 200, r.content
    profile = u.compliance_profile
    assert (profile.pan_number or "") == ""
    assert profile.aadhar_number


@pytest.mark.django_db
def test_compliance_submit_pan_number_without_document_fails(system_config, primary_ebook):
    cfg = get_system_config()
    cfg.trigger_instant_kyc_submission = True
    cfg.save(update_fields=["trigger_instant_kyc_submission"])
    doc = _compliance_legal_doc()

    u = _member_user("+919887766632")
    _paid_order_for_kyc(u, primary_ebook)
    client = APIClient()
    client.force_authenticate(user=u)
    _accept_compliance_agreements(client, u, doc)

    payload = _compliance_submit_payload(
        pan_number=unique_test_pan(),
        name_on_pan="Test User",
        aadhar_number=unique_test_aadhaar(seq=632),
    )
    payload.pop("pan_document", None)

    r = client.post("/api/v1/auth/compliance/submit/", payload, format="multipart")
    assert r.status_code == 400
    assert "pan_document" in (r.json().get("message") or "").lower()


@pytest.mark.django_db
def test_compliance_submit_bank_locked_after_first_approval(system_config, primary_ebook):
    cfg = get_system_config()
    cfg.trigger_instant_kyc_submission = True
    cfg.save(update_fields=["trigger_instant_kyc_submission"])
    doc = _compliance_legal_doc()

    u = _member_user("+919887766633")
    _paid_order_for_kyc(u, primary_ebook)
    client = APIClient()
    client.force_authenticate(user=u)
    _accept_compliance_agreements(client, u, doc)

    payload = _compliance_submit_payload(aadhar_number=unique_test_aadhaar(seq=633))
    assert client.post("/api/v1/auth/compliance/submit/", payload, format="multipart").status_code == 200

    u.kyc_first_approved_at = timezone.now()
    u.save(update_fields=["kyc_first_approved_at"])
    profile = u.compliance_profile
    original_acct = profile.account_number

    changed = _compliance_submit_payload(
        aadhar_number=profile.aadhar_number,
        account_number="99999999999",
        nominee_name="Updated Nominee",
    )
    r = client.post("/api/v1/auth/compliance/submit/", changed, format="multipart")
    assert r.status_code == 400
    assert "Bank details cannot be changed" in (r.json().get("message") or "")

    profile.refresh_from_db()
    assert profile.account_number == original_acct

    same_bank = _compliance_submit_payload(
        aadhar_number=profile.aadhar_number,
        account_number=original_acct,
        ifsc=profile.ifsc,
        bank_name=profile.bank_name,
        branch=profile.branch,
        account_holder_name=profile.account_holder_name,
        nominee_name="Updated Nominee",
    )
    r_ok = client.post("/api/v1/auth/compliance/submit/", same_bank, format="multipart")
    assert r_ok.status_code == 200, r_ok.content
    profile.refresh_from_db()
    assert profile.account_number == original_acct
    assert profile.nominee_name == "Updated Nominee"


@pytest.mark.django_db
def test_compliance_submit_upi_qr_saved_in_status(system_config, primary_ebook):
    cfg = get_system_config()
    cfg.trigger_instant_kyc_submission = True
    cfg.save(update_fields=["trigger_instant_kyc_submission"])
    doc = _compliance_legal_doc()

    u = _member_user("+919887766634")
    _paid_order_for_kyc(u, primary_ebook)
    client = APIClient()
    client.force_authenticate(user=u)
    _accept_compliance_agreements(client, u, doc)

    payload = _compliance_submit_payload(aadhar_number=unique_test_aadhaar(seq=634))
    payload["upi_qr"] = SimpleUploadedFile(
        "upi_qr.png", b"\x89PNG", content_type="image/png"
    )
    r = client.post("/api/v1/auth/compliance/submit/", payload, format="multipart")
    assert r.status_code == 200, r.content

    status_r = client.get("/api/v1/auth/kyc/status/")
    assert status_r.status_code == 200
    bank = status_r.json()["data"]["compliance_submission"]["bank_details"]
    assert bank.get("upi_qr_url")
    assert bank.get("bank_details_locked") is False
