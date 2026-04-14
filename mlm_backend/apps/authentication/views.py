from django.conf import settings
from django.contrib.auth import authenticate
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from apps.audit.services import write_audit
from apps.common.responses import envelope_response
from apps.users.models import User
from apps.users.services import allocate_member_identity, resolve_sponsor_by_code

from .models import OTPRecord
from .otp import can_send_otp, create_otp_record, register_otp_send, verify_otp
from .serializers import (
    AdminLoginSerializer,
    BankSerializer,
    KYCSubmitSerializer,
    SendOTPSerializer,
    VerifyLoginSerializer,
    VerifyRegisterSerializer,
)


def _tokens_for(user):
    refresh = RefreshToken.for_user(user)
    return {
        "refresh": str(refresh),
        "access": str(refresh.access_token),
    }


class LogoutView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request: Request):
        refresh = request.data.get("refresh")
        if refresh:
            try:
                token = RefreshToken(refresh)
                token.blacklist()
            except Exception:
                pass
        return envelope_response(None, message="Logged out")


logout = LogoutView.as_view()


def _mask_aadhaar(raw: str) -> str:
    d = "".join(c for c in raw if c.isdigit())
    if len(d) < 4:
        return "XXXX-XXXX-XXXX"
    return f"XXXX-XXXX-{d[-4:]}"


@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def send_otp(request: Request):
    ser = SendOTPSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    phone = (ser.validated_data.get("phone") or "").strip() or None
    email = (ser.validated_data.get("email") or "").strip().lower() or None
    purpose_raw = ser.validated_data["purpose"]
    purpose_map = {
        "REGISTER": OTPRecord.Purpose.REGISTER,
        "LOGIN": OTPRecord.Purpose.LOGIN,
        "KYC": OTPRecord.Purpose.KYC,
        "ADMIN_LOGIN": OTPRecord.Purpose.ADMIN_LOGIN,
    }
    purpose = purpose_map[purpose_raw]
    ident = phone or email
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
        purpose=purpose,
        ip=request.META.get("REMOTE_ADDR"),
    )
    register_otp_send(ident)
    write_audit("otp.sent", payload={"purpose": purpose_raw}, ip_address=request.META.get("REMOTE_ADDR"))
    data = {"expires_in_seconds": 600}
    if settings.DEBUG:
        data["dev_otp"] = rec.otp_code
    return envelope_response(data, message="OTP sent")


@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def verify_otp_register(request: Request):
    ser = VerifyRegisterSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    phone = (ser.validated_data.get("phone") or "").strip() or None
    email = (ser.validated_data.get("email") or "").strip().lower() or None
    code = ser.validated_data["otp_code"]
    full_name = ser.validated_data["full_name"]
    ref = (ser.validated_data.get("referral_code") or "").strip()

    rec, err = verify_otp(
        phone=phone,
        email=email,
        code=code,
        purpose=OTPRecord.Purpose.REGISTER,
    )
    if err:
        return envelope_response(None, message=err, success=False, status=400)

    if User.objects.filter(phone=phone).exists() or (
        email and User.objects.filter(email=email).exists()
    ):
        return envelope_response(None, message="User already exists", success=False, status=400)

    sponsor = resolve_sponsor_by_code(ref) if ref else None
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

    return envelope_response({"user": _user_payload(user), "tokens": _tokens_for(user)}, message="Logged in")


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


@api_view(["GET", "PATCH"])
@permission_classes([permissions.IsAuthenticated])
def me(request: Request):
    user = request.user
    if request.method == "GET":
        return envelope_response(_user_payload(user))
    for field in ["full_name", "payout_preference"]:
        if field in request.data:
            setattr(user, field, request.data[field])
    user.save()
    return envelope_response(_user_payload(user), message="Updated")


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def kyc_submit(request: Request):
    ser = KYCSubmitSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    user = request.user
    user.pan_number = ser.validated_data["pan_number"].upper()
    user.aadhaar_number = _mask_aadhaar(ser.validated_data["aadhaar_number"])
    user.kyc_status = User.KYCStatus.PENDING
    user.save()
    return envelope_response({"kyc_status": user.kyc_status}, message="KYC submitted")


@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def kyc_status(request: Request):
    u = request.user
    return envelope_response(
        {"kyc_status": u.kyc_status, "pan_last4": (u.pan_number or "")[-4:]},
    )


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def bank_update(request: Request):
    ser = BankSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    u = request.user
    for k, v in ser.validated_data.items():
        setattr(u, k, v)
    u.save()
    return envelope_response({"ok": True})


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
    return send_otp(request)


@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def admin_verify_otp(request: Request):
    ser = VerifyLoginSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    email = ser.validated_data.get("email")
    code = ser.validated_data["otp_code"]
    rec, err = verify_otp(email=email, code=code, purpose=OTPRecord.Purpose.ADMIN_LOGIN)
    if err:
        return envelope_response(None, message=err, success=False, status=400)
    user = User.objects.filter(email=email, is_staff=True).first()
    if not user:
        return envelope_response(None, message="Forbidden", success=False, status=403)
    return envelope_response({"tokens": _tokens_for(user)}, message="Admin 2FA OK")
