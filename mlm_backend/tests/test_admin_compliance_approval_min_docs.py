import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.agreements.models import MemberComplianceProfile
from apps.users.models import User
from apps.users.services import allocate_member_identity
from tests.conftest import unique_test_aadhaar, unique_test_pan


def _member(phone: str) -> User:
    mid, ref, link = allocate_member_identity()
    u = User(
        phone=phone,
        full_name="M",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
    )
    u.set_unusable_password()
    u.save()
    return u


@pytest.mark.django_db
def test_admin_compliance_approve_requires_min_docs(system_config):
    admin_mid, admin_ref, admin_link = allocate_member_identity()
    admin = User(
        phone="+919000009999",
        full_name="Admin",
        member_id=admin_mid,
        referral_code=admin_ref,
        referral_link=admin_link,
        is_staff=True,
        role=User.Role.SUPPORT,
    )
    admin.set_unusable_password()
    admin.save()

    u = _member("+919000000111")
    MemberComplianceProfile.objects.create(user=u)  # empty profile: missing docs

    client = APIClient()
    client.force_authenticate(user=admin)

    r = client.post(f"/api/v1/admin/users/{u.id}/compliance/approve/", {}, format="json")
    assert r.status_code == 400
    body = r.json()
    assert body["success"] is False
    assert "Missing" in (body.get("message") or "")

    # Add minimum docs/fields and approve.
    profile = u.compliance_profile
    profile.pan_number = unique_test_pan()
    profile.aadhar_number = unique_test_aadhaar()
    profile.pan_document = SimpleUploadedFile("pan.pdf", b"fake", content_type="application/pdf")
    profile.aadhar_front = SimpleUploadedFile(
        "aad_front.pdf", b"fake", content_type="application/pdf"
    )
    profile.aadhar_back = SimpleUploadedFile(
        "aad_back.pdf", b"fake", content_type="application/pdf"
    )
    profile.save()

    r2 = client.post(f"/api/v1/admin/users/{u.id}/compliance/approve/", {}, format="json")
    assert r2.status_code == 200
    u.refresh_from_db()
    assert u.kyc_status == User.KYCStatus.VERIFIED

