import logging
import uuid

from django.conf import settings
from django.db import transaction
from rest_framework import permissions, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.request import Request

from apps.audit.services import write_audit
from apps.authentication.models import OTPRecord
from apps.authentication.otp import (
    can_send_otp,
    create_otp_record,
    otp_send_rate_limit_message,
    register_otp_send,
    verify_otp,
)
from apps.common.client_ip import get_client_ip
from apps.common.permissions import IsSuperAdmin
from apps.common.responses import envelope_response
from .models import (
    AgreementCategory,
    LegalDocument,
    MemberComplianceProfile,
    UserAgreementAcceptance,
)
from .serializers import (
    AgreementOTPSendSerializer,
    AgreementOTPVerifySerializer,
    ComplianceSubmitSerializer,
    LegalDocumentAdminSerializer,
    LegalDocumentPublicSerializer,
)
from .services import (
    apply_profile_bank_to_user,
    record_agreement_acceptances,
    snapshot_profile_hashes,
    sync_identity_to_user,
    touch_compliance_submit_user_state,
    user_has_required_acceptances,
    user_missing_acceptances,
)

_logger = logging.getLogger(__name__)


def _agreement_otp_payload(rec: OTPRecord) -> dict:
    data: dict = {"expires_in_seconds": 600}
    if getattr(settings, "EXPOSE_OTP_IN_RESPONSE", True):
        data["otp"] = rec.otp_code
        _logger.info(
            "OTP sent purpose=AGREEMENT otp=%s user_id=%s",
            rec.otp_code,
            (rec.payload or {}).get("user_id"),
        )
    return data


@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def legal_documents_public_list(request: Request):
    qs = LegalDocument.objects.filter(is_active=True)
    category = (request.query_params.get("category") or "").strip()
    if category:
        qs = qs.filter(category__iexact=category)
    qs = qs.order_by("category", "name")
    ser = LegalDocumentPublicSerializer(
        qs, many=True, context={"request": request}
    )
    return envelope_response({"results": ser.data})


@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def legal_documents_compliance_legal_list(request: Request):
    """Active agreements that are legal documents and require compliance acceptance."""
    qs = LegalDocument.objects.filter(
        is_active=True,
        requires_acceptance_for_compliance=True,
        category__iexact=AgreementCategory.LEGAL_DOCUMENT,
    ).order_by("name")
    doc_ids = list(qs.values_list("id", flat=True))
    accepted_versions: dict[int, str] = {}
    if doc_ids:
        rows = (
            UserAgreementAcceptance.objects.filter(
                user_id=request.user.id,
                document_id__in=doc_ids,
            )
            .order_by("document_id", "-accepted_at")
            .values_list("document_id", "version_accepted")
        )
        for document_id, version_accepted in rows:
            if document_id not in accepted_versions:
                accepted_versions[document_id] = version_accepted
    ser = LegalDocumentPublicSerializer(
        qs,
        many=True,
        context={"request": request, "accepted_versions": accepted_versions},
    )
    return envelope_response({"results": ser.data})


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def agreement_send_otp(request: Request):
    user = request.user
    ser = AgreementOTPSendSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    doc_ids = list(dict.fromkeys(ser.validated_data["document_ids"]))
    docs = list(
        LegalDocument.objects.filter(
            id__in=doc_ids,
            is_active=True,
            requires_acceptance_for_compliance=True,
        )
    )
    if len(docs) != len(doc_ids):
        return envelope_response(
            None,
            message="One or more document_ids are invalid or do not require acceptance.",
            success=False,
            status=400,
        )
    phone = (user.phone or "").strip() or None
    email = (user.email or "").strip().lower() or None
    ident = phone or email
    if not ident:
        return envelope_response(
            None,
            message="Your account must have a phone or email to verify agreements.",
            success=False,
            status=400,
        )
    if not can_send_otp(ident):
        return envelope_response(
            None,
            message=otp_send_rate_limit_message(),
            success=False,
            errors={"detail": "rate_limited"},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )
    payload = {
        "document_ids": sorted(doc_ids),
        "user_id": user.id,
    }
    rec = create_otp_record(
        phone=phone,
        email=email,
        purpose=OTPRecord.Purpose.AGREEMENT,
        ip=get_client_ip(request),
        payload=payload,
    )
    register_otp_send(ident)
    write_audit(
        "agreement.otp_sent",
        actor=user,
        payload={"document_ids": doc_ids},
        ip_address=get_client_ip(request),
    )
    return envelope_response(
        _agreement_otp_payload(rec),
        message="OTP sent",
    )


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def agreement_verify(request: Request):
    user = request.user
    ser = AgreementOTPVerifySerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    doc_ids = sorted(list(dict.fromkeys(ser.validated_data["document_ids"])))
    code = ser.validated_data["otp_code"]
    phone = (user.phone or "").strip() or None
    email = (user.email or "").strip().lower() or None
    rec, err = verify_otp(
        phone=phone,
        email=email,
        code=code,
        purpose=OTPRecord.Purpose.AGREEMENT,
    )
    if err:
        return envelope_response(None, message=err, success=False, status=400)
    got_p = rec.payload or {}
    if int(got_p.get("user_id") or 0) != user.id:
        return envelope_response(None, message="Invalid Otp", success=False, status=400)
    if sorted(got_p.get("document_ids") or []) != doc_ids:
        return envelope_response(
            None,
            message="document_ids do not match this OTP session.",
            success=False,
            status=400,
        )
    docs = list(
        LegalDocument.objects.filter(
            id__in=doc_ids,
            is_active=True,
            requires_acceptance_for_compliance=True,
        )
    )
    if len(docs) != len(doc_ids):
        return envelope_response(
            None,
            message="Invalid documents for acceptance.",
            success=False,
            status=400,
        )
    batch = uuid.uuid4()
    ip = get_client_ip(request)
    with transaction.atomic():
        record_agreement_acceptances(
            user=user,
            documents=docs,
            acceptance_batch_id=batch,
            ip=ip,
        )
    write_audit(
        "agreement.accepted",
        actor=user,
        payload={"document_ids": doc_ids, "batch": str(batch)},
        ip_address=ip,
    )
    return envelope_response(
        {"acceptance_batch_id": str(batch), "accepted_document_ids": doc_ids},
        message="Agreements accepted",
    )


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def compliance_submit(request: Request):
    user = request.user
    if not user_has_required_acceptances(user):
        return envelope_response(
            {
                "missing_acceptances": user_missing_acceptances(user),
            },
            message="Accept all required agreements before submitting documents.",
            success=False,
            status=400,
        )
    ser = ComplianceSubmitSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    data = ser.validated_data

    pan_f = request.FILES.get("pan_document")
    aad_front_f = request.FILES.get("aadhar_front")
    aad_back_f = request.FILES.get("aadhar_back")

    profile = getattr(user, "compliance_profile", None)
    existing_pan = bool(profile and profile.pan_document)
    existing_aadhaar_front = bool(profile and profile.aadhar_front)
    existing_aadhaar_back = bool(profile and profile.aadhar_back)
    if not pan_f and not existing_pan:
        return envelope_response(
            None,
            message="pan_document file is required.",
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

    with transaction.atomic():
        profile, _ = MemberComplianceProfile.objects.select_for_update().get_or_create(
            user=user
        )
        before = snapshot_profile_hashes(profile)

        profile.date_of_birth = data["date_of_birth"]
        profile.gender = data["gender"]
        profile.full_address = data["full_address"].strip()
        profile.city = data["city"].strip()
        profile.pin_code = data["pin_code"].strip()
        profile.state = data["state"].strip()
        profile.country = data["country"].strip()

        profile.pan_number = data["pan_number"]
        profile.name_on_pan = data["name_on_pan"].strip()
        profile.aadhar_number = data["aadhar_number"]
        profile.name_on_aadhar = data["name_on_aadhar"].strip()

        profile.nominee_name = data["nominee_name"].strip()
        profile.nominee_relationship = data["nominee_relationship"].strip()
        profile.nominee_phone = data["nominee_phone"]
        profile.nominee_date_of_birth = data["nominee_date_of_birth"]

        profile.account_holder_name = data["account_holder_name"].strip()
        profile.account_number = data["account_number"].strip()
        profile.bank_name = data["bank_name"].strip()
        profile.ifsc = data["ifsc"]
        profile.branch = data["branch"].strip()
        profile.account_type = data["account_type"]
        profile.payout_preference = data.get("payout_preference") or "UPI"

        if pan_f:
            profile.pan_document = pan_f
        if aad_front_f:
            profile.aadhar_front = aad_front_f
        if aad_back_f:
            profile.aadhar_back = aad_back_f

        profile.save()

        after = snapshot_profile_hashes(profile)
        material_changed = before != after

        apply_profile_bank_to_user(user, profile, data.get("upi_id") or "")
        sync_identity_to_user(user, profile, data["aadhar_number"])

        touch_compliance_submit_user_state(user=user)

    write_audit(
        "compliance.submitted",
        actor=user,
        payload={"material_changed": material_changed},
        ip_address=get_client_ip(request),
    )
    user.refresh_from_db()
    return envelope_response(
        {
            "kyc_status": user.kyc_status,
            "compliance_submission_version": user.compliance_submission_version,
        },
        message="Compliance submitted; pending admin review.",
    )


@api_view(["GET", "POST"])
@permission_classes([IsSuperAdmin])
def admin_legal_documents(request: Request):
    if request.method == "GET":
        qs = LegalDocument.objects.order_by("-id")
        ser = LegalDocumentAdminSerializer(
            qs, many=True, context={"request": request}
        )
        return envelope_response({"results": ser.data})
    ser = LegalDocumentAdminSerializer(data=request.data, context={"request": request})
    ser.is_valid(raise_exception=True)
    ser.save()
    return envelope_response(ser.data, message="Created", status=201)


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsSuperAdmin])
def admin_legal_document_detail(request: Request, pk: int):
    doc = LegalDocument.objects.filter(pk=pk).first()
    if not doc:
        return envelope_response(None, message="Not found", success=False, status=404)
    if request.method == "GET":
        return envelope_response(
            LegalDocumentAdminSerializer(doc, context={"request": request}).data
        )
    if request.method == "DELETE":
        doc.is_active = False
        doc.save(update_fields=["is_active", "updated_at"])
        return envelope_response(None, message="Deactivated")
    ser = LegalDocumentAdminSerializer(
        doc,
        data=request.data,
        partial=True,
        context={"request": request},
    )
    ser.is_valid(raise_exception=True)
    ser.save()
    return envelope_response(ser.data, message="Updated")
