"""SUPER_ADMIN self-service KYC from Admin Profile (OTP + auto-approve)."""

from __future__ import annotations

from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.request import Request

from apps.agreements.serializers import ComplianceSubmitSerializer
from apps.agreements.services import (
    apply_profile_bank_to_user,
    bank_details_locked,
    bank_submission_differs_from_profile,
    finalize_super_admin_kyc_auto_approve,
    snapshot_profile_hashes,
    sync_identity_to_user,
)
from apps.audit.services import write_audit
from apps.authentication.models import OTPRecord
from apps.authentication.otp import (
    can_send_otp,
    create_otp_record,
    otp_send_rate_limit_message,
    register_otp_send,
    send_or_expose_otp,
    verify_otp,
)
from apps.common.client_ip import get_client_ip
from apps.common.permissions import IsSuperAdmin
from apps.common.responses import envelope_response
from apps.users.models import User

ADMIN_KYC_DECLARATION = (
    "I confirm that the KYC and compliance information submitted for my "
    "Company Administrator account is true and complete."
)


def _require_super_admin_user(request: Request) -> User | None:
    u = request.user
    if not u or not getattr(u, "is_authenticated", False):
        return None
    if getattr(u, "role", None) != User.Role.SUPER_ADMIN:
        return None
    return u


def _user_otp_identifiers(user: User) -> tuple[str | None, str | None, str | None]:
    phone = (user.phone or "").strip() or None
    email = (user.email or "").strip().lower() or None
    ident = phone or email
    return phone, email, ident


@api_view(["POST"])
@permission_classes([IsSuperAdmin])
def admin_profile_kyc_send_otp(request: Request):
    user = _require_super_admin_user(request)
    if not user:
        return envelope_response(
            None,
            message="Forbidden",
            success=False,
            status=status.HTTP_403_FORBIDDEN,
        )
    phone, email, ident = _user_otp_identifiers(user)
    if not ident:
        return envelope_response(
            None,
            message="Add a phone number or email to your profile before KYC verification.",
            success=False,
            errors={"detail": "contact_required"},
            status=status.HTTP_403_FORBIDDEN,
        )
    if (
        user.account_status in (User.AccountStatus.SUSPENDED, User.AccountStatus.DEACTIVATED)
        or not user.is_active
    ):
        return envelope_response(
            None,
            message="Account is not active.",
            success=False,
            errors={"detail": "account_suspended"},
            status=status.HTTP_403_FORBIDDEN,
        )
    if not can_send_otp(ident):
        return envelope_response(
            None,
            message=otp_send_rate_limit_message(),
            success=False,
            errors={"detail": "rate_limited"},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )
    rec = create_otp_record(
        phone=phone,
        email=email,
        purpose=OTPRecord.Purpose.ADMIN_KYC,
        ip=request.META.get("REMOTE_ADDR"),
        payload={
            "user_id": user.id,
            "purpose": "admin_kyc_submit",
            "declaration": ADMIN_KYC_DECLARATION,
        },
    )
    register_otp_send(ident)
    write_audit(
        "admin_kyc.otp_sent",
        actor=user,
        payload={"user_id": user.id},
        ip_address=get_client_ip(request),
    )
    data = send_or_expose_otp(
        rec,
        full_name=user.full_name or "",
        email=email,
        phone=phone,
        purpose_label="ADMIN_KYC",
        recipient_hint=ident,
    )
    return envelope_response(data, message="OTP sent")


admin_profile_kyc_send_otp.view_class.throttle_scope = "otp_send"


@api_view(["POST"])
@permission_classes([IsSuperAdmin])
def admin_profile_kyc_submit(request: Request):
    user = _require_super_admin_user(request)
    if not user:
        return envelope_response(
            None,
            message="Forbidden",
            success=False,
            status=status.HTTP_403_FORBIDDEN,
        )

    otp_code = (request.data.get("otp_code") or "").strip()
    if not otp_code:
        return envelope_response(
            None,
            message="otp_code is required.",
            success=False,
            errors={"otp_code": "required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    phone, email, _ident = _user_otp_identifiers(user)
    rec, err = verify_otp(
        phone=phone,
        email=email,
        code=otp_code,
        purpose=OTPRecord.Purpose.ADMIN_KYC,
    )
    if err:
        return envelope_response(None, message=err, success=False, status=status.HTTP_400_BAD_REQUEST)
    got_p = rec.payload or {}
    if int(got_p.get("user_id") or 0) != user.id:
        return envelope_response(None, message="Invalid Otp", success=False, status=status.HTTP_400_BAD_REQUEST)

    ser = ComplianceSubmitSerializer(
        data=request.data,
        context={"request": request, "user": user},
    )
    ser.is_valid(raise_exception=True)
    data = ser.validated_data

    pan_f = request.FILES.get("pan_document")
    aad_front_f = request.FILES.get("aadhar_front")
    aad_back_f = request.FILES.get("aadhar_back")
    upi_qr_f = request.FILES.get("upi_qr")

    from apps.agreements.models import MemberComplianceProfile

    profile = MemberComplianceProfile.objects.filter(user=user).first()
    existing_pan = bool(profile and profile.pan_document)
    existing_aadhaar_front = bool(profile and profile.aadhar_front)
    existing_aadhaar_back = bool(profile and profile.aadhar_back)
    pan_provided = bool((data.get("pan_number") or "").strip())
    if pan_provided and not pan_f and not existing_pan:
        return envelope_response(
            None,
            message="pan_document file is required when PAN number is provided.",
            success=False,
            status=400,
        )
    if not aad_front_f and not existing_aadhaar_front:
        return envelope_response(
            None,
            message="aadhar_front file is required.",
            success=False,
            status=400,
        )
    if not aad_back_f and not existing_aadhaar_back:
        return envelope_response(
            None,
            message="aadhar_back file is required.",
            success=False,
            status=400,
        )

    was_reapproval = bool(user.kyc_first_approved_at)
    material_changed = False

    with transaction.atomic():
        profile, _ = MemberComplianceProfile.objects.select_for_update().get_or_create(user=user)
        before = snapshot_profile_hashes(profile)

        locked = bank_details_locked(user, profile)
        if locked and bank_submission_differs_from_profile(
            profile,
            data,
            user,
            upi_qr_uploaded=bool(upi_qr_f),
        ):
            return envelope_response(
                None,
                message=(
                    "Bank details cannot be changed after KYC approval. "
                    "Contact support to update your bank information."
                ),
                success=False,
                status=400,
            )

        profile.date_of_birth = data["date_of_birth"]
        profile.gender = data["gender"]
        profile.full_address = data["full_address"].strip()
        profile.city = data["city"].strip()
        profile.pin_code = data["pin_code"].strip()
        profile.state = data["state"].strip()
        profile.country = data["country"].strip()

        profile.pan_number = (data.get("pan_number") or "").strip()
        profile.name_on_pan = (data.get("name_on_pan") or "").strip()
        profile.aadhar_number = data["aadhar_number"]
        profile.name_on_aadhar = data["name_on_aadhar"].strip()

        profile.nominee_name = data["nominee_name"].strip()
        profile.nominee_relationship = data["nominee_relationship"].strip()
        profile.nominee_phone = data["nominee_phone"]
        profile.nominee_date_of_birth = data["nominee_date_of_birth"]

        if not locked:
            profile.account_holder_name = data["account_holder_name"].strip()
            profile.account_number = data["account_number"].strip()
            profile.bank_name = data["bank_name"].strip()
            profile.ifsc = data["ifsc"]
            profile.branch = data["branch"].strip()
            profile.account_type = data["account_type"]
            profile.payout_preference = data.get("payout_preference") or "UPI"
            if upi_qr_f:
                profile.upi_qr = upi_qr_f

        if pan_f:
            profile.pan_document = pan_f
        if aad_front_f:
            profile.aadhar_front = aad_front_f
        if aad_back_f:
            profile.aadhar_back = aad_back_f

        profile.save()

        after = snapshot_profile_hashes(profile)
        material_changed = before != after

        if not locked:
            apply_profile_bank_to_user(user, profile, data.get("upi_id") or "")
        sync_identity_to_user(user, profile, data["aadhar_number"])
        user.save(
            update_fields=[
                "pan_number",
                "aadhaar_number",
                "payout_preference",
                "bank_account_number",
                "bank_ifsc",
                "bank_name",
                "upi_id",
                "updated_at",
            ]
        )

        finalize_super_admin_kyc_auto_approve(user=user, is_reapproval=was_reapproval)

    write_audit(
        "compliance.admin_self_approved",
        actor=user,
        payload={
            "material_changed": material_changed,
            "is_reapproval": was_reapproval,
            "declaration": ADMIN_KYC_DECLARATION,
        },
        ip_address=get_client_ip(request),
    )
    user.refresh_from_db()
    return envelope_response(
        {
            "kyc_status": user.kyc_status,
            "kyc_first_approved_at": (
                user.kyc_first_approved_at.isoformat() if user.kyc_first_approved_at else None
            ),
            "compliance_submission_version": user.compliance_submission_version,
        },
        message="KYC verified successfully.",
    )


admin_profile_kyc_submit.view_class.throttle_scope = "otp_verify"
