import logging

from django.conf import settings
from django.contrib.auth import authenticate
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import AccessToken

from apps.audit.services import write_audit
from apps.common.responses import envelope_response
from apps.agreements.models import MemberComplianceProfile
from apps.agreements.services import user_missing_acceptances
from apps.users.models import User
from apps.users.services import allocate_member_identity, resolve_sponsor_by_code

from .models import OTPRecord
from .otp import can_send_otp, create_otp_record, register_otp_send, verify_otp
from .serializers import (
    AdminLoginSerializer,
    SendOTPSerializer,
    SendRegisterOTPSerializer,
    VerifyLoginSerializer,
    VerifyRegisterCompleteSerializer,
)

_logger = logging.getLogger(__name__)


def _otp_send_payload(rec: OTPRecord, *, purpose_label: str, recipient_hint: str) -> dict:
    """expires_in_seconds + optional otp/log when EXPOSE_OTP_IN_RESPONSE is on."""
    data: dict = {"expires_in_seconds": 600}
    if getattr(settings, "EXPOSE_OTP_IN_RESPONSE", True):
        data["otp"] = rec.otp_code
        _logger.info(
            "OTP sent purpose=%s otp=%s to=%s",
            purpose_label,
            rec.otp_code,
            recipient_hint or "?",
        )
    return data


def _tokens_for(user):
    """Single access JWT (lifetime from SIMPLE_JWT / JWT_ACCESS_TOKEN_LIFETIME_MINUTES)."""
    return {"access": str(AccessToken.for_user(user))}


def _session_role(user: User) -> str:
    """Normalized role label for login responses."""
    return "admin" if getattr(user, "is_staff", False) else "user"


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
    data = _otp_send_payload(
        rec,
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
    data = _otp_send_payload(
        rec,
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
    return envelope_response({"user": _user_payload(user), "tokens": _tokens_for(user)}, message="Registered")


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
    if err == "invalid_otp" and user:
        # track failed attempts on user simplified: lock after 5 failures in session — omitted
        pass
    if err:
        return envelope_response(None, message=err, success=False, status=400)

    if not user:
        user = User.objects.filter(phone=phone).first() if phone else User.objects.filter(email=email).first()
    if not user:
        return envelope_response(None, message="User not found", success=False, status=404)

    return envelope_response(
        {
            "user": _user_payload(user),
            "tokens": _tokens_for(user),
            "role": _session_role(user),
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


def _me_payload(user: User):
    profile = MemberComplianceProfile.objects.filter(user=user).first()
    personal_information = {
        "full_name": user.full_name or None,
        "email_address": user.email or None,
        "mobile_number": user.phone or None,
        "date_of_birth": _fmt_ddmmyyyy(profile.date_of_birth) if profile else None,
        "gender": profile.get_gender_display() if profile and profile.gender else None,
    }
    member_information = {
        "member_id": user.member_id or None,
        "joined_date": user.created_at.date().isoformat() if user.created_at else None,
        "address": profile.full_address if profile and profile.full_address else None,
        "state": profile.state if profile and profile.state else None,
        "pin_code": profile.pin_code if profile and profile.pin_code else None,
        "country": profile.country if profile and profile.country else None,
    }
    data = _user_payload(user)
    data["personal_information"] = personal_information
    data["member_information"] = member_information
    return data


@api_view(["GET", "PATCH"])
@permission_classes([permissions.IsAuthenticated])
def me(request: Request):
    user = request.user
    if request.method == "GET":
        return envelope_response(_me_payload(user))
    for field in ["full_name", "payout_preference"]:
        if field in request.data:
            setattr(user, field, request.data[field])
    user.save()
    return envelope_response(_user_payload(user), message="Updated")


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
@permission_classes([permissions.IsAuthenticated])
def kyc_status(request: Request):
    u = request.user
    missed = user_missing_acceptances(u)
    has_profile = MemberComplianceProfile.objects.filter(user=u).exists()
    return envelope_response(
        {
            "kyc_status": u.kyc_status,
            "kyc_submitted_at": u.kyc_submitted_at.isoformat()
            if u.kyc_submitted_at
            else None,
            "kyc_reviewed_at": u.kyc_reviewed_at.isoformat()
            if u.kyc_reviewed_at
            else None,
            "kyc_rejection_reason": u.kyc_rejection_reason or None,
            "pan_last4": (u.pan_number or "")[-4:],
            "missing_acceptances": missed,
            "has_compliance_profile": bool(has_profile),
            "compliance_submission_version": u.compliance_submission_version,
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


@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def validate_referral(request: Request):
    code = request.data.get("referral_code") or request.data.get("code")
    s = resolve_sponsor_by_code(code or "")
    if not s:
        return envelope_response(None, message="Invalid code", success=False, status=404)
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
    if not can_send_otp(ident):
        return envelope_response(
            None,
            message="OTP rate limit: max 3 per 10 minutes",
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
    data = _otp_send_payload(
        rec,
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
    return envelope_response(
        {
            "tokens": _tokens_for(user),
            "role": _session_role(user),
        },
        message="Admin 2FA OK",
    )
