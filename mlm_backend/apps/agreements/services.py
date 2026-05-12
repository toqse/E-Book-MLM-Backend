import uuid
from typing import Optional

from django.utils import timezone

from apps.common.aadhaar_utils import mask_aadhaar_display
from apps.agreements.models import LegalDocument, MemberComplianceProfile, UserAgreementAcceptance
from apps.users.models import User


def accepted_version_for_document(user_id: int, document_id: int) -> Optional[str]:
    row = (
        UserAgreementAcceptance.objects.filter(user_id=user_id, document_id=document_id)
        .order_by("-accepted_at")
        .values_list("version_accepted", flat=True)
        .first()
    )
    return row


def user_missing_acceptances(user: User) -> list[dict]:
    """Docs that need acceptance at current LegalDocument.version."""
    required = LegalDocument.objects.filter(
        is_active=True,
        requires_acceptance_for_compliance=True,
    )
    missing: list[dict] = []
    for doc in required:
        accepted = accepted_version_for_document(user.id, doc.id)
        if accepted != doc.version:
            missing.append(
                {
                    "id": doc.id,
                    "name": doc.name,
                    "required_version": doc.version,
                    "accepted_version": accepted,
                }
            )
    return missing


def user_has_required_acceptances(user: User) -> bool:
    return len(user_missing_acceptances(user)) == 0


def record_agreement_acceptances(
    *,
    user: User,
    documents: list[LegalDocument],
    acceptance_batch_id: uuid.UUID,
    ip: Optional[str],
) -> None:
    rows = [
        UserAgreementAcceptance(
            user=user,
            document=d,
            version_accepted=d.version,
            acceptance_batch_id=acceptance_batch_id,
            accepted_ip=ip or None,
        )
        for d in documents
    ]
    UserAgreementAcceptance.objects.bulk_create(rows)


def touch_compliance_submit_user_state(*, user: User):
    """After every compliance submit: awaiting review; clears prior verified/rejected review state."""
    user.kyc_status = User.KYCStatus.PENDING
    user.kyc_submitted_at = timezone.now()
    user.kyc_reviewed_at = None
    user.kyc_rejection_reason = ""
    user.compliance_submission_version += 1
    user.save()


def snapshot_profile_hashes(profile: MemberComplianceProfile) -> dict:
    """Primitive change detection for resubmission / PENDING rules."""
    return {
        "pan": profile.pan_number,
        "aadhaar": profile.aadhar_number,
        "acct": profile.account_number,
        "nominee": profile.nominee_name + profile.nominee_phone,
        "pk": str(profile.pk or ""),
        "pan_f": getattr(profile.pan_document, "name", "") or "",
        "aar_front_f": getattr(profile.aadhar_front, "name", "") or "",
        "aar_back_f": getattr(profile.aadhar_back, "name", "") or "",
    }


def apply_profile_bank_to_user(user: User, profile: MemberComplianceProfile, upi_override: str = ""):
    user.payout_preference = profile.payout_preference
    user.bank_account_number = profile.account_number or ""
    user.bank_ifsc = (profile.ifsc or "").strip().upper() or ""
    user.bank_name = (profile.bank_name or "").strip() or ""
    if upi_override is not None:
        user.upi_id = (upi_override or "").strip()


def sync_identity_to_user(user: User, profile: MemberComplianceProfile, raw_aadhaar_digits: str):
    user.pan_number = (profile.pan_number or "").strip().upper() or ""
    user.aadhaar_number = mask_aadhaar_display(raw_aadhaar_digits)
