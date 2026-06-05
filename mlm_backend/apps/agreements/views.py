import logging
import uuid

from django.conf import settings
from django.db import transaction
from django.http import FileResponse
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
    send_or_expose_otp,
    verify_otp,
)
from apps.common.client_ip import get_client_ip
from apps.common.permissions import IsSuperAdmin
from apps.common.responses import envelope_response
from apps.users.models import User
from .models import (
    AgreementCategory,
    LegalDocument,
    MemberComplianceProfile,
    UserAgreementAcceptance,
    UserAgreementAcceptanceDeclaration,
)
from .proof_download_token import parse_proof_download_token
from .proof_service import ensure_proof_for_batch
from .user_docs import build_agreements_user_array
from .serializers import (
    AgreementOTPSendSerializer,
    AgreementOTPVerifySerializer,
    ComplianceRequiredAgreementAdminSerializer,
    ComplianceSubmitSerializer,
    LegalDocumentAdminSerializer,
    LegalDocumentPublicSerializer,
)
from apps.users.kyc_eligibility import (
    kyc_submission_blocked_response,
    user_kyc_submission_allowed,
)
from .services import (
    apply_profile_bank_to_user,
    clear_other_compliance_required_flags,
    get_compliance_required_legal_document,
    record_agreement_acceptances,
    snapshot_profile_hashes,
    sync_identity_to_user,
    touch_compliance_submit_user_state,
    user_has_required_acceptances,
    user_missing_acceptances,
)

_logger = logging.getLogger(__name__)

def _normalize_category_query(raw: str) -> str:
    """
    Map a user-provided category query to a canonical AgreementCategory value.

    Used for GET list filters; admin create/update uses serializer validation.
    """

    s = (raw or "").strip()
    if not s:
        return ""

    def _norm(v: str) -> str:
        v = (v or "").casefold()
        v = v.replace("&", " and ")
        cleaned = "".join(ch if ch.isalnum() else " " for ch in v)
        return " ".join(cleaned.split())

    needle = _norm(s)
    for choice in AgreementCategory.values:
        if needle == _norm(choice):
            return choice
    return s


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
    return envelope_response(
        {
            "results": ser.data,
            "user": build_agreements_user_array(request, request.user),
        }
    )


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
    user = request.user
    return envelope_response(
        {
            "results": ser.data,
            "user": {
                "id": user.pk,
                "full_name": user.full_name,
            },
        }
    )


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def agreement_send_otp(request: Request):
    user = request.user
    if not user_kyc_submission_allowed(user):
        return kyc_submission_blocked_response()
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
    declaration = ser.validated_data["declaration"]
    payload = {
        "document_ids": sorted(doc_ids),
        "user_id": user.id,
        "declaration": declaration,
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
        send_or_expose_otp(
            rec,
            full_name=user.full_name or "",
            email=email,
            phone=phone,
            purpose_label="AGREEMENT",
            recipient_hint=ident,
        ),
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
    if "declaration" not in got_p:
        return envelope_response(
            None,
            message="This OTP session has no declaration. Request a new code from "
            "POST /api/v1/agreements/send-otp/ including a declaration text.",
            success=False,
            status=400,
        )
    declaration_text = (got_p.get("declaration") or "").strip()
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
        UserAgreementAcceptanceDeclaration.objects.create(
            user=user,
            acceptance_batch_id=batch,
            declaration_text=declaration_text,
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


agreement_send_otp.view_class.throttle_scope = "otp_send"
agreement_verify.view_class.throttle_scope = "otp_verify"


@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def agreement_acceptance_proof_download(request: Request, acceptance_batch_id: uuid.UUID):
    """
    Download the stored agreement acceptance proof PDF for a batch (attachment disposition).

    Auth: either ``Authorization: Bearer <access>`` (same user as batch owner), or a signed
    ``token`` query string returned in ``pdf_download_url`` from GET /api/v1/agreements/
    (so opening the link in a browser works without custom headers).
    """
    user: User | None = None
    if getattr(request.user, "is_authenticated", False):
        user = request.user
    else:
        raw = (request.query_params.get("token") or "").strip()
        if not raw:
            return envelope_response(
                None,
                message=(
                    "Authentication credentials were not provided. "
                    "Open the full pdf_download_url from GET /api/v1/agreements/ "
                    "(it includes a signed token query parameter), "
                    "or send Authorization: Bearer <access_token>."
                ),
                success=False,
                status=401,
                errors={"detail": "Authentication credentials were not provided."},
            )
        parsed = parse_proof_download_token(raw)
        if parsed is None:
            return envelope_response(
                None,
                message="Invalid or expired download link. Refresh agreements and use the new pdf_download_url.",
                success=False,
                status=401,
            )
        uid, bid_from_token = parsed
        if bid_from_token != acceptance_batch_id:
            return envelope_response(
                None,
                message="Invalid download link.",
                success=False,
                status=400,
            )
        user = User.objects.filter(pk=uid).first()
        if user is None:
            return envelope_response(None, message="Not found", success=False, status=404)

    if not UserAgreementAcceptance.objects.filter(
        user=user, acceptance_batch_id=acceptance_batch_id
    ).exists():
        return envelope_response(None, message="Not found", success=False, status=404)
    proof = ensure_proof_for_batch(user, acceptance_batch_id)
    if not proof or not (getattr(proof.pdf_file, "name", None) or "").strip():
        return envelope_response(None, message="Proof not available", success=False, status=404)
    short = str(acceptance_batch_id).replace("-", "")[:8]
    filename = f"agreement-acceptance-proof-{short}.pdf"
    return FileResponse(
        proof.pdf_file.open("rb"),
        as_attachment=True,
        filename=filename,
        content_type="application/pdf",
    )


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def compliance_submit(request: Request):
    user = request.user
    if not user_kyc_submission_allowed(user):
        return kyc_submission_blocked_response()
    if not user_has_required_acceptances(user):
        return envelope_response(
            {
                "missing_acceptances": user_missing_acceptances(user),
            },
            message="Accept all required agreements before submitting documents.",
            success=False,
            status=400,
        )
    ser = ComplianceSubmitSerializer(
        data=request.data,
        context={"request": request, "user": user},
    )
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
        category = _normalize_category_query(request.query_params.get("category") or "")
        if category:
            qs = qs.filter(category__iexact=category)
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


def _compliance_required_singleton_queryset():
    return LegalDocument.objects.filter(
        requires_acceptance_for_compliance=True,
        category__iexact=AgreementCategory.LEGAL_DOCUMENT,
    )


@api_view(["GET", "POST", "PUT", "PATCH", "DELETE"])
@permission_classes([IsSuperAdmin])
def admin_compliance_required_agreement(request: Request):
    """
    Singleton CRUD for the one legal document members must accept for compliance.

    POST/PUT upsert the same row (create on first call, update thereafter).
    Clears requires_acceptance_for_compliance on any other LegalDocument rows.
    """
    ctx = {"request": request}

    if request.method == "GET":
        doc = get_compliance_required_legal_document()
        if not doc:
            return envelope_response(None)
        return envelope_response(
            ComplianceRequiredAgreementAdminSerializer(doc, context=ctx).data
        )

    if request.method == "DELETE":
        with transaction.atomic():
            doc = (
                _compliance_required_singleton_queryset()
                .select_for_update()
                .order_by("-id")
                .first()
            )
            if not doc:
                return envelope_response(None, message="Not found", success=False, status=404)
            doc.is_active = False
            doc.requires_acceptance_for_compliance = False
            doc.save(
                update_fields=["is_active", "requires_acceptance_for_compliance", "updated_at"]
            )
        return envelope_response(None, message="Deactivated")

    partial = request.method == "PATCH"
    is_create_upsert = request.method in ("POST", "PUT")

    with transaction.atomic():
        existing = (
            _compliance_required_singleton_queryset()
            .select_for_update()
            .order_by("-id")
            .first()
        )
        if existing:
            ser = ComplianceRequiredAgreementAdminSerializer(
                existing,
                data=request.data,
                partial=partial,
                context=ctx,
            )
        elif is_create_upsert:
            ser = ComplianceRequiredAgreementAdminSerializer(
                data=request.data,
                context=ctx,
            )
        else:
            return envelope_response(
                None,
                message="No compliance-required agreement exists. Use POST to create.",
                success=False,
                status=404,
            )
        ser.is_valid(raise_exception=True)
        doc = ser.save()
        if not doc.is_active:
            doc.is_active = True
            doc.save(update_fields=["is_active", "updated_at"])
        clear_other_compliance_required_flags(exclude_pk=doc.pk)

    message = "Updated" if existing else "Created"
    status_code = 200 if existing else 201
    return envelope_response(ser.data, message=message, status=status_code)
