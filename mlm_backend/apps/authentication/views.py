import logging

from django.conf import settings
from django.contrib.auth import authenticate
from django.db import IntegrityError
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import AccessToken

from apps.audit.services import write_audit
from apps.common.responses import envelope_response
from apps.common.url_utils import public_media_url
from apps.payments.models import Order, OrderLine
from apps.agreements.models import AgreementCategory, LegalDocument, MemberComplianceProfile
from apps.agreements.services import accepted_version_for_document, user_missing_acceptances
from apps.admin_panel.utils import get_system_config
from apps.mlm_tree.models import BinaryNode
from apps.agreements.kyc_invite_token import parse_kyc_invite_token
from apps.users.kyc_eligibility import (
    user_kyc_access_context,
    user_kyc_invitation_should_send,
    user_kyc_submission_allowed,
)
from apps.users.models import AccountDeletionRequest, User
from apps.users import team_services
from apps.users.services import (
    allocate_member_identity,
    effective_company_referral_code,
    environment_company_referral_code,
    is_account_capped,
    resolve_sponsor_by_code,
)
from apps.wallet.services.member_money import build_band_ladder, get_wallet_row

from .models import OTPRecord
from .otp import (
    can_send_otp,
    create_otp_record,
    otp_send_rate_limit_message,
    register_otp_send,
    send_or_expose_otp,
    verify_otp,
)
from .serializers import (
    AdminLoginSerializer,
    ProfileUpdateSerializer,
    SendOTPSerializer,
    SendRegisterOTPSerializer,
    VerifyLoginSerializer,
    VerifyRegisterCompleteSerializer,
)

_logger = logging.getLogger(__name__)


def _user_has_paid_ebook_purchase(user: User) -> bool:
    """True when the user has at least one PAID order that includes an ebook (legacy or cart line items)."""
    return (
        Order.objects.filter(user=user, status=Order.Status.PAID)
        .filter(Q(ebook_id__isnull=False) | Exists(OrderLine.objects.filter(order_id=OuterRef("pk"))))
        .exists()
    )


def _tokens_for(user):
    """Single access JWT (lifetime from SIMPLE_JWT / JWT_ACCESS_TOKEN_LIFETIME_MINUTES)."""
    return {"access": str(AccessToken.for_user(user))}


def _session_role(user: User) -> str:
    """Normalized role label for login responses."""
    return "admin" if getattr(user, "is_staff", False) else "user"


def _user_by_phone_or_email(phone: str | None, email: str | None) -> User | None:
    if phone:
        return User.objects.filter(phone=phone).first()
    if email:
        return User.objects.filter(email=email).first()
    return None


class LogoutView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request: Request):
        # Stateless access-only JWT: discard client-side after this call (no refresh blacklist).
        return envelope_response(None, message="Logged out")


logout = LogoutView.as_view()


@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def send_otp(request: Request):
    ser = SendOTPSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    phone = (ser.validated_data.get("phone") or "").strip() or None
    email = (ser.validated_data.get("email") or "").strip().lower() or None
    purpose_raw = ser.validated_data["purpose"]
    purpose_map = {
        "LOGIN": OTPRecord.Purpose.LOGIN,
        "KYC": OTPRecord.Purpose.KYC,
        "ADMIN_LOGIN": OTPRecord.Purpose.ADMIN_LOGIN,
    }
    purpose = purpose_map[purpose_raw]
    ident = phone or email
    u = None

    if purpose in (OTPRecord.Purpose.LOGIN, OTPRecord.Purpose.KYC):
        u = _user_by_phone_or_email(phone, email)
        if not u:
            return envelope_response(
                None,
                message="User not found",
                success=False,
                status=status.HTTP_404_NOT_FOUND,
            )
        if (
            u.account_status in (User.AccountStatus.SUSPENDED, User.AccountStatus.DEACTIVATED)
            or not u.is_active
        ):
            return envelope_response(
                None,
                message="Account suspended",
                success=False,
                errors={"detail": "account_suspended"},
                status=status.HTTP_403_FORBIDDEN,
            )
    elif purpose == OTPRecord.Purpose.ADMIN_LOGIN:
        u = _user_by_phone_or_email(phone, email)
        if not u or not u.is_staff:
            return envelope_response(
                None,
                message="Forbidden",
                success=False,
                status=status.HTTP_403_FORBIDDEN,
            )
        if (
            u.account_status in (User.AccountStatus.SUSPENDED, User.AccountStatus.DEACTIVATED)
            or not u.is_active
        ):
            return envelope_response(
                None,
                message="Account suspended",
                success=False,
                errors={"detail": "account_suspended"},
                status=status.HTTP_403_FORBIDDEN,
            )

    if not can_send_otp(ident):
        return envelope_response(
            None,
            message="OTP limit exceeded. Try again later",
            success=False,
            errors={"detail": "rate_limited"},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )
    rec = create_otp_record(
        phone=phone,
        email=email,
        purpose=purpose,
        ip=request.META.get("REMOTE_ADDR"),
    )
    register_otp_send(ident)
    write_audit("otp.sent", payload={"purpose": purpose_raw}, ip_address=request.META.get("REMOTE_ADDR"))
    user_name = ""
    user_email = email
    user_phone = phone
    if purpose in (OTPRecord.Purpose.LOGIN, OTPRecord.Purpose.KYC, OTPRecord.Purpose.ADMIN_LOGIN):
        if u:
            user_name = u.full_name or ""
            user_email = user_email or (u.email or None)
            user_phone = user_phone or (u.phone or None)
    data = send_or_expose_otp(
        rec,
        full_name=user_name,
        email=user_email,
        phone=user_phone,
        purpose_label=purpose_raw,
        recipient_hint=ident or "",
    )
    return envelope_response(data, message="OTP sent")


@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def send_register_otp(request: Request):
    """Send OTP for signup; full_name, referral_code (+ optional email) submitted here."""
    ser = SendRegisterOTPSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    phone = ser.validated_data["phone"]
    email = ser.validated_data.get("email")
    full_name = ser.validated_data["full_name"].strip()
    referral_code = ser.validated_data["referral_code"]
    if not full_name:
        return envelope_response(
            None,
            message="Full name is required.",
            success=False,
            status=status.HTTP_400_BAD_REQUEST,
        )

    sponsor = resolve_sponsor_by_code(referral_code)
    if not sponsor:
        return envelope_response(
            None,
            message="Invalid referral code",
            success=False,
            status=400,
        )
    if is_account_capped(sponsor):
        return envelope_response(
            None,
            message="This referral link is no longer active",
            success=False,
            status=400,
        )
    if User.objects.filter(phone=phone).exists():
        return envelope_response(None, message="User already exists", success=False, status=400)
    if email and User.objects.filter(email=email).exists():
        return envelope_response(None, message="User already exists", success=False, status=400)

    ident = phone
    if not can_send_otp(ident):
        return envelope_response(
            None,
            message="OTP limit exceeded. Try again later",
            success=False,
            errors={"detail": "rate_limited"},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    rec = create_otp_record(
        phone=phone,
        email=None,
        purpose=OTPRecord.Purpose.REGISTER,
        ip=request.META.get("REMOTE_ADDR"),
        registration_full_name=full_name,
        registration_email=email,
        registration_referral_code=referral_code,
        registration_sponsor=sponsor,
    )
    register_otp_send(ident)
    write_audit(
        "otp.sent",
        payload={"purpose": "REGISTER"},
        ip_address=request.META.get("REMOTE_ADDR"),
    )
    data = send_or_expose_otp(
        rec,
        full_name=full_name,
        email=email,
        phone=phone,
        purpose_label="REGISTER",
        recipient_hint=phone,
    )
    return envelope_response(data, message="OTP sent")


@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def verify_otp_register(request: Request):
    """Complete signup using phone + otp only (profile fields captured at register/send-otp)."""
    ser = VerifyRegisterCompleteSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    phone = ser.validated_data["phone"]
    code = ser.validated_data["otp_code"]

    rec, err = verify_otp(
        phone=phone,
        email=None,
        code=code,
        purpose=OTPRecord.Purpose.REGISTER,
    )
    if err:
        return envelope_response(None, message=err, success=False, status=400)

    full_name = (rec.registration_full_name or "").strip()
    sponsor = rec.registration_sponsor
    email = (
        rec.registration_email.strip().lower()
        if rec.registration_email
        else None
    )
    if not full_name or not sponsor:
        return envelope_response(
            None,
            message="Registration session invalid. Request a new registration OTP.",
            success=False,
            status=400,
        )

    if User.objects.filter(phone=phone).exists():
        return envelope_response(None, message="User already exists", success=False, status=400)
    if email and User.objects.filter(email=email).exists():
        return envelope_response(None, message="User already exists", success=False, status=400)

    member_id, referral_code, referral_link = allocate_member_identity()
    user = User(
        phone=phone,
        email=email,
        full_name=full_name,
        member_id=member_id,
        referral_code=referral_code,
        referral_link=referral_link,
        sponsor=sponsor,
    )
    user.set_unusable_password()
    user.save()
    write_audit("user.registered", actor=user, target_type="User", target_id=user.id)
    return envelope_response(
        {
            "user": _user_payload(user),
            "tokens": _tokens_for(user),
            "is_book_purchased": _user_has_paid_ebook_purchase(user),
        },
        message="Registered",
    )


@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def verify_otp_login(request: Request):
    ser = VerifyLoginSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    phone = (ser.validated_data.get("phone") or "").strip() or None
    email = (ser.validated_data.get("email") or "").strip().lower() or None
    code = ser.validated_data["otp_code"]

    ident = phone or email
    user = User.objects.filter(phone=phone).first() if phone else User.objects.filter(email=email).first()
    if user and user.otp_locked_until and user.otp_locked_until > timezone.now():
        return envelope_response(None, message="Account locked", success=False, status=423)

    rec, err = verify_otp(phone=phone, email=email, code=code, purpose=OTPRecord.Purpose.LOGIN)
    if err == "Invalid Otp" and user:
        # track failed attempts on user simplified: lock after 5 failures in session — omitted
        pass
    if err:
        return envelope_response(None, message=err, success=False, status=400)

    if not user:
        user = User.objects.filter(phone=phone).first() if phone else User.objects.filter(email=email).first()
    if not user:
        return envelope_response(None, message="User not found", success=False, status=404)

    if user.account_status in (User.AccountStatus.SUSPENDED, User.AccountStatus.DEACTIVATED) or not user.is_active:
        return envelope_response(
            None,
            message="Account suspended",
            success=False,
            errors={"detail": "account_suspended"},
            status=403,
        )

    return envelope_response(
        {
            "user": _user_payload(user),
            "tokens": _tokens_for(user),
            "role": _session_role(user),
            "is_book_purchased": _user_has_paid_ebook_purchase(user),
        },
        message="Logged in",
    )


def _user_payload(user: User):
    return {
        "id": user.id,
        "member_id": user.member_id,
        "full_name": user.full_name,
        "phone": user.phone,
        "email": user.email,
        "referral_code": user.referral_code,
        "is_member": user.is_member,
        "role": user.role,
    }


def _fmt_ddmmyyyy(date_obj):
    if not date_obj:
        return None
    return date_obj.strftime("%d/%m/%Y")


def _abs_media_url(request: Request, filefield) -> str | None:
    return public_media_url(request, filefield)


def _compliance_submitted_payload(
    request: Request, user: User, profile: MemberComplianceProfile | None
) -> dict | None:
    """Compliance snapshot grouped for KYC status: personal, KYC, bank, nominee."""
    if not profile:
        return None
    return {
        "personal_details": {
            "full_name": user.full_name or None,
            "email": user.email or None,
            "phone": user.phone or None,
            "date_of_birth": _fmt_ddmmyyyy(profile.date_of_birth),
            "gender": profile.gender,
            "full_address": profile.full_address,
            "city": profile.city,
            "pin_code": profile.pin_code,
            "state": profile.state,
            "country": profile.country,
        },
        "kyc_details": {
            "pan_number": profile.pan_number,
            "name_on_pan": profile.name_on_pan,
            "aadhar_number": profile.aadhar_number,
            "name_on_aadhar": profile.name_on_aadhar,
            "verification_documents": {
                "pan_document_url": _abs_media_url(request, profile.pan_document),
                "aadhar_front_url": _abs_media_url(request, profile.aadhar_front),
                "aadhar_back_url": _abs_media_url(request, profile.aadhar_back),
                "aadhar_document_url": _abs_media_url(request, profile.aadhar_document),
            },
        },
        "bank_details": {
            "account_holder_name": profile.account_holder_name,
            "account_number": profile.account_number,
            "bank_name": profile.bank_name,
            "ifsc": profile.ifsc,
            "branch": profile.branch,
            "account_type": profile.account_type,
            "payout_preference": profile.payout_preference,
            "upi_id": user.upi_id or None,
        },
        "nominee_details": {
            "nominee_name": profile.nominee_name,
            "nominee_relationship": profile.nominee_relationship,
            "nominee_phone": profile.nominee_phone,
            "nominee_date_of_birth": _fmt_ddmmyyyy(profile.nominee_date_of_birth),
        },
        "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
    }


def _me_payload(user: User):
    user = User.objects.select_related("sponsor").filter(pk=user.pk).first() or user
    profile = MemberComplianceProfile.objects.filter(user=user).first()
    kyc_ctx = user_kyc_access_context(user)
    pan_present = bool((user.pan_number or "").strip())
    kyc_verified = user.kyc_status == User.KYCStatus.VERIFIED
    mlm_unlocked = kyc_ctx["mlm_features_unlocked"]

    personal_information = {
        "full_name": user.full_name or None,
        "email_address": user.email or None,
        "mobile_number": user.phone or None,
        "date_of_birth": _fmt_ddmmyyyy(profile.date_of_birth) if profile else None,
        "gender": profile.get_gender_display() if profile and profile.gender else None,
    }
    member_information = {
        "member_id": user.member_id or None,
        "joined_date": _fmt_ddmmyyyy(user.created_at.date()) if user.created_at else None,
        "address": profile.full_address if profile and profile.full_address else None,
        "city": profile.city if profile and profile.city else None,
        "state": profile.state if profile and profile.state else None,
        "pin_code": profile.pin_code if profile and profile.pin_code else None,
        "country": profile.country if profile and profile.country else None,
    }
    data = _user_payload(user)
    if not mlm_unlocked:
        data["referral_code"] = None
    data["is_book_purchased"] = _user_has_paid_ebook_purchase(user)
    data["is_kyc_account_approved"] = bool(getattr(user, "kyc_first_approved_at", None))
    data["personal_information"] = personal_information
    data["member_information"] = member_information
    data["display"] = {
        "profile_initial": (user.full_name or "").strip()[:1].upper() or None,
        "avatar_url": None,
    }
    kyc_eligible_at = kyc_ctx.get("kyc_eligible_at")
    data["account_status"] = {
        "account_status": user.account_status,
        "account_status_label": user.get_account_status_display() if user.account_status else None,
        "kyc_status": user.kyc_status,
        "kyc_status_label": user.get_kyc_status_display() if user.kyc_status else None,
        "pan_submitted": pan_present,
        "tds_rate_percent": None,
        "withdrawals_blocked": not kyc_verified,
        "kyc_submission_allowed": kyc_ctx["kyc_submission_allowed"],
        "kyc_submission_mode": kyc_ctx["kyc_submission_mode"],
        "trigger_instant_kyc_submission": kyc_ctx["trigger_instant_kyc_submission"],
        "kyc_eligible_at": kyc_eligible_at.isoformat() if kyc_eligible_at else None,
        "kyc_invitation_sent_at": (
            user.kyc_invitation_sent_at.isoformat() if user.kyc_invitation_sent_at else None
        ),
        "referral_link": None,
        "referral_link_active": False,
    }
    data["feature_access"] = {
        "team_network": mlm_unlocked,
        "earnings": mlm_unlocked,
        "withdrawals": mlm_unlocked and user.account_status == User.AccountStatus.ACTIVE,
        "milestones": mlm_unlocked,
        "sponsor_slots": mlm_unlocked,
        "referral_program": mlm_unlocked,
        "compliance_submit": kyc_ctx["kyc_submission_allowed"]
        and user.kyc_status != User.KYCStatus.VERIFIED,
    }
    data["tax_withholding"] = None
    data["withdrawal_band"] = None
    data["earning_cap"] = None
    data["kyc_notice"] = {
        "message": kyc_ctx.get("kyc_notice_message"),
        "code": kyc_ctx.get("kyc_notice_code"),
    }
    data["sponsor"] = (
        {
            "member_id": user.sponsor.member_id,
            "full_name": user.sponsor.full_name,
        }
        if user.sponsor_id and user.sponsor
        else None
    )
    data["binary_placement"] = None
    data["team_legs"] = None

    if mlm_unlocked:
        wallet = get_wallet_row(user)
        cfg = get_system_config()
        band_ladder = build_band_ladder(wallet, cfg)
        current_band = next((b for b in band_ladder if b.get("is_current")), None)
        ctx = team_services.build_subtree_context(user)
        bn = BinaryNode.objects.filter(user_id=user.pk).only("position", "level").first()

        used = wallet.total_earned
        cap = cfg.earning_cap
        used_percent = float((used / cap) * 100) if cap and cap > 0 else 0.0
        remaining = cap - used if cap and cap > 0 else 0

        if pan_present and kyc_verified:
            tds_rate_percent = float(cfg.tds_194h_rate) * 100
            tds_rate_reason = "PAN on file"
        else:
            tds_rate_percent = 20.0
            tds_rate_reason = "PAN not available"

        data["account_status"]["tds_rate_percent"] = tds_rate_percent
        data["account_status"]["referral_link"] = user.referral_link or None
        data["account_status"]["referral_link_active"] = bool(
            user.is_active and user.account_status == User.AccountStatus.ACTIVE
        )
        data["tax_withholding"] = {
            "tds_rate_percent": tds_rate_percent,
            "reason": tds_rate_reason,
        }
        data["withdrawal_band"] = {
            "current_band": current_band,
            "bands": band_ladder,
        }
        data["earning_cap"] = {
            "limit": str(cap),
            "used": str(used),
            "used_percent": round(used_percent, 2),
            "remaining": str(max(0, remaining)),
        }
        data["binary_placement"] = {
            "position": bn.position if bn else None,
            "level": bn.level if bn else None,
            "position_label": (
                f"{bn.get_position_display()} (L{bn.level})"
                if bn and bn.position and bn.level
                else None
            ),
        }
        weaker = team_services.suggested_leg(ctx.left_leg_count, ctx.right_leg_count)
        data["team_legs"] = {
            "left_leg_count": ctx.left_leg_count,
            "right_leg_count": ctx.right_leg_count,
            "weaker_leg": weaker,
            "left_leg_label": "strong"
            if ctx.left_leg_count > ctx.right_leg_count
            else "weak"
            if ctx.left_leg_count < ctx.right_leg_count
            else "neutral",
            "right_leg_label": "strong"
            if ctx.right_leg_count > ctx.left_leg_count
            else "weak"
            if ctx.right_leg_count < ctx.left_leg_count
            else "neutral",
            "subtree_member_count": max(0, len(ctx.subtree_user_ids) - 1)
            if ctx.subtree_user_ids
            else 0,
        }
    if user.is_staff and getattr(user, "role", None) in (
        User.Role.SUPER_ADMIN,
        User.Role.FINANCE,
        User.Role.SUPPORT,
    ):
        data["admin"] = {
            "default_company_referral_code": effective_company_referral_code(),
            "default_company_referral_code_environment": environment_company_referral_code(),
        }
    if user_kyc_invitation_should_send(user):
        from apps.users.tasks import send_kyc_invitation_for_user

        send_kyc_invitation_for_user.delay(user.pk)
    if user.account_status == User.AccountStatus.CAPPED:
        data["referral_code"] = None
        data["account_status"]["referral_link"] = None
        data["account_status"]["referral_link_active"] = False
        data["profile_message"] = (
            "Your account has reached the earning cap and is now inactive."
        )
    return data


@api_view(["GET", "PATCH"])
@permission_classes([permissions.IsAuthenticated])
def me(request: Request):
    user = request.user
    if request.method == "GET":
        return envelope_response(_me_payload(user))
    data_in = request.data or {}
    if "default_company_referral_code" in data_in:
        if getattr(user, "role", None) != User.Role.SUPER_ADMIN:
            return envelope_response(
                None,
                message="Forbidden",
                success=False,
                errors={"detail": "super_admin_only"},
                status=status.HTTP_403_FORBIDDEN,
            )
        raw = data_in.get("default_company_referral_code")
        if raw is not None and not isinstance(raw, str):
            return envelope_response(
                None,
                message="Invalid default_company_referral_code",
                success=False,
                status=status.HTTP_400_BAD_REQUEST,
            )
        code = (raw or "").strip()
        if len(code) > 64:
            return envelope_response(
                None,
                message="default_company_referral_code too long",
                success=False,
                errors={"default_company_referral_code": "max_length_64"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        cfg = get_system_config()
        cfg.default_company_referral_code = code
        cfg.updated_by = user
        cfg.save()
    payload = {k: request.data[k] for k in request.data if k != "default_company_referral_code"}
    ser = ProfileUpdateSerializer(data=payload, partial=True, context={"user": user})
    ser.is_valid(raise_exception=True)
    data = ser.validated_data

    for field in ["full_name", "email", "payout_preference"]:
        if field in data:
            setattr(user, field, data[field])

    profile_map = {
        "date_of_birth": "date_of_birth",
        "gender": "gender",
        "address": "full_address",
        "city": "city",
        "pin_code": "pin_code",
        "state": "state",
        "country": "country",
    }
    profile_fields = [k for k in profile_map if k in data]
    profile = None
    if profile_fields:
        profile, _ = MemberComplianceProfile.objects.get_or_create(user=user)
        for input_field in profile_fields:
            setattr(profile, profile_map[input_field], data[input_field])

    user.save()
    if profile:
        profile.save()
    return envelope_response(_me_payload(user), message="Updated")


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def account_deletion_request(request: Request):
    u: User = request.user
    data = request.data or {}
    reason = (data.get("reason") if isinstance(data, dict) else None) or ""
    reason = str(reason).strip()
    if not reason:
        return envelope_response(
            None,
            message="reason is required",
            success=False,
            errors={"reason": ["This field may not be blank."]},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if AccountDeletionRequest.objects.filter(
        user=u,
        status=AccountDeletionRequest.Status.PENDING,
    ).exists():
        return envelope_response(
            None,
            message="You already have a pending account deletion request.",
            success=False,
            errors={"detail": "pending_account_deletion_exists"},
            status=status.HTTP_409_CONFLICT,
        )

    try:
        row = AccountDeletionRequest.objects.create(
            user=u,
            snapshot_member_id=u.member_id,
            snapshot_full_name=u.full_name,
            snapshot_email=u.email,
            snapshot_phone=u.phone,
            reason=reason,
            status=AccountDeletionRequest.Status.PENDING,
        )
    except IntegrityError:
        return envelope_response(
            None,
            message="You already have a pending account deletion request.",
            success=False,
            errors={"detail": "pending_account_deletion_exists"},
            status=status.HTTP_409_CONFLICT,
        )

    return envelope_response(
        {
            "id": row.id,
            "status": row.status,
            "reason": row.reason,
            "created_at": row.created_at.isoformat(),
        },
        message="Account deletion request submitted",
        status=status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def kyc_submit(request: Request):
    return envelope_response(
        None,
        message=(
            "Deprecated. Use multipart POST /api/v1/auth/compliance/submit/ "
            "(agreements OTP + acceptance required)."
        ),
        success=False,
        status=status.HTTP_410_GONE,
    )


@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def kyc_invite_validate(request: Request):
    raw = (request.query_params.get("token") or "").strip()
    uid = parse_kyc_invite_token(raw) if raw else None
    if uid is None:
        return envelope_response(
            None,
            message="Invalid or expired KYC invitation link.",
            success=False,
            errors={"detail": "invalid_kyc_invite_token"},
            status=400,
        )
    user = User.objects.filter(pk=uid).only("id", "member_id", "full_name", "kyc_status").first()
    if user is None:
        return envelope_response(None, message="Not found", success=False, status=404)
    from apps.users.kyc_eligibility import user_kyc_submission_mode

    return envelope_response(
        {
            "valid": True,
            "kyc_submission_allowed": user_kyc_submission_allowed(user),
            "kyc_submission_mode": user_kyc_submission_mode(),
            "kyc_status": user.kyc_status,
            "member_id": user.member_id,
            "full_name": user.full_name,
            "redirect_hint": "compliance",
        },
    )


@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def kyc_status(request: Request):
    u = request.user
    missed = user_missing_acceptances(u)
    profile = MemberComplianceProfile.objects.filter(user=u).first()
    compliance_legal_required = LegalDocument.objects.filter(
        is_active=True,
        requires_acceptance_for_compliance=True,
        category__iexact=AgreementCategory.LEGAL_DOCUMENT,
    )
    compliance_legal_missing = [
        doc.id
        for doc in compliance_legal_required
        if accepted_version_for_document(u.id, doc.id) != doc.version
    ]
    is_agreement_accepted = len(compliance_legal_missing) == 0
    kyc_ctx = user_kyc_access_context(u)
    return envelope_response(
        {
            "kyc_status": u.kyc_status,
            "kyc_submission_allowed": kyc_ctx["kyc_submission_allowed"],
            "kyc_submission_mode": kyc_ctx["kyc_submission_mode"],
            "kyc_submitted_at": u.kyc_submitted_at.isoformat()
            if u.kyc_submitted_at
            else None,
            "kyc_reviewed_at": u.kyc_reviewed_at.isoformat()
            if u.kyc_reviewed_at
            else None,
            "kyc_rejection_reason": u.kyc_rejection_reason or None,
            "pan_last4": (u.pan_number or "")[-4:],
            "missing_acceptances": missed,
            "is_agreement_accepted": is_agreement_accepted,
            "has_compliance_profile": bool(profile),
            "compliance_submission_version": u.compliance_submission_version,
            "compliance_submission": _compliance_submitted_payload(request, u, profile),
        },
    )


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def bank_update(request: Request):
    return envelope_response(
        None,
        message=(
            "Deprecated. Use multipart POST /api/v1/auth/compliance/submit/ "
            "with bank and payout fields."
        ),
        success=False,
        status=status.HTTP_410_GONE,
    )


@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def company_referral_code_public(request: Request):
    """Active company referral code for signup (SystemConfig override or env default)."""
    return envelope_response(
        {"default_company_referral_code": effective_company_referral_code()},
    )


@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def validate_referral(request: Request):
    code = request.data.get("referral_code") or request.data.get("code")
    s = resolve_sponsor_by_code(code or "")
    if not s:
        return envelope_response(None, message="Invalid code", success=False, status=404)
    if is_account_capped(s):
        return envelope_response(
            None,
            message="This referral link is no longer active",
            success=False,
            status=404,
        )
    return envelope_response({"sponsor_name": s.full_name})


@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def admin_password_login(request: Request):
    ser = AdminLoginSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    user = authenticate(
        request,
        username=ser.validated_data["email"],
        password=ser.validated_data["password"],
    )
    if not user or not user.is_staff:
        return envelope_response(None, message="Invalid credentials", success=False, status=401)
    if user.account_status in (User.AccountStatus.SUSPENDED, User.AccountStatus.DEACTIVATED) or not user.is_active:
        return envelope_response(
            None,
            message="Account suspended",
            success=False,
            errors={"detail": "account_suspended"},
            status=403,
        )
    return envelope_response({"tokens": _tokens_for(user)}, message="Admin step 1 OK")


@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def admin_send_otp(request: Request):
    ser = SendOTPSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    phone = (ser.validated_data.get("phone") or "").strip() or None
    email = (ser.validated_data.get("email") or "").strip().lower() or None
    ident = phone or email
    if not ident:
        return envelope_response(
            None, message="Provide phone or email", success=False, status=400
        )
    u = _user_by_phone_or_email(phone, email)
    if not u or not u.is_staff:
        return envelope_response(
            None, message="Forbidden", success=False, status=status.HTTP_403_FORBIDDEN
        )
    if (
        u.account_status in (User.AccountStatus.SUSPENDED, User.AccountStatus.DEACTIVATED)
        or not u.is_active
    ):
        return envelope_response(
            None,
            message="Account suspended",
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
        purpose=OTPRecord.Purpose.ADMIN_LOGIN,
        ip=request.META.get("REMOTE_ADDR"),
    )
    register_otp_send(ident)
    write_audit(
        "otp.sent",
        payload={"purpose": "ADMIN_LOGIN"},
        ip_address=request.META.get("REMOTE_ADDR"),
    )
    data = send_or_expose_otp(
        rec,
        full_name=u.full_name or "",
        email=email or (u.email or None),
        phone=phone or (u.phone or None),
        purpose_label="ADMIN_LOGIN",
        recipient_hint=ident,
    )
    return envelope_response(data, message="OTP sent")


@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def admin_verify_otp(request: Request):
    ser = VerifyLoginSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    phone = (ser.validated_data.get("phone") or "").strip() or None
    email = ser.validated_data.get("email")
    code = ser.validated_data["otp_code"]
    rec, err = verify_otp(
        phone=phone,
        email=email,
        code=code,
        purpose=OTPRecord.Purpose.ADMIN_LOGIN,
    )
    if err:
        return envelope_response(None, message=err, success=False, status=400)
    user = (
        User.objects.filter(phone=phone, is_staff=True).first()
        if phone
        else User.objects.filter(email=email, is_staff=True).first()
    )
    if not user:
        return envelope_response(None, message="Forbidden", success=False, status=403)
    if user.account_status in (User.AccountStatus.SUSPENDED, User.AccountStatus.DEACTIVATED) or not user.is_active:
        return envelope_response(
            None,
            message="Account suspended",
            success=False,
            errors={"detail": "account_suspended"},
            status=403,
        )
    return envelope_response(
        {
            "tokens": _tokens_for(user),
            "role": _session_role(user),
        },
        message="Admin 2FA OK",
    )


# Scoped DRF throttling (defense-in-depth; OTP send also uses DB limiter in otp.py).
send_otp.view_class.throttle_scope = "otp_send"
send_register_otp.view_class.throttle_scope = "otp_send"
verify_otp_register.view_class.throttle_scope = "otp_verify"
verify_otp_login.view_class.throttle_scope = "otp_verify"
admin_send_otp.view_class.throttle_scope = "otp_send"
admin_verify_otp.view_class.throttle_scope = "otp_verify"
admin_password_login.view_class.throttle_scope = "auth_login"
