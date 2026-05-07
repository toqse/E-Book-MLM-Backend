from datetime import datetime

from rest_framework import serializers

from apps.common.phone_utils import normalize_phone_registration
from apps.users.models import User


class SendOTPSerializer(serializers.Serializer):
    phone = serializers.CharField(required=False, allow_blank=True, max_length=22)
    email = serializers.EmailField(required=False, allow_blank=True)
    purpose = serializers.ChoiceField(
        choices=["LOGIN", "KYC", "ADMIN_LOGIN"], default="LOGIN"
    )

    def validate(self, attrs):
        purpose = attrs.get("purpose") or "LOGIN"
        phone_raw = attrs.get("phone") or ""
        phone_raw = phone_raw.strip() if isinstance(phone_raw, str) else ""
        email = (attrs.get("email") or "").strip().lower() or None

        phone = None
        if phone_raw:
            try:
                phone = normalize_phone_registration(phone_raw)
            except ValueError as exc:
                raise serializers.ValidationError(str(exc)) from exc

        if not phone and not email:
            raise serializers.ValidationError("Provide phone or email")
        attrs["purpose"] = purpose
        attrs["phone"] = phone
        attrs["email"] = email
        return attrs


class SendRegisterOTPSerializer(serializers.Serializer):
    """International phone E.164 with leading +; referral + full_name captured before OTP."""

    phone = serializers.CharField(max_length=22)
    email = serializers.EmailField(required=False, allow_blank=True)
    full_name = serializers.CharField(max_length=255)
    referral_code = serializers.CharField(max_length=32)

    def validate_phone(self, value):
        try:
            return normalize_phone_registration(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate(self, attrs):
        email = attrs.get("email")
        if isinstance(email, str):
            e = email.strip().lower()
            attrs["email"] = e or None
        attrs["referral_code"] = (attrs.get("referral_code") or "").strip()
        if not attrs["referral_code"]:
            raise serializers.ValidationError(
                {"referral_code": "Referral code is required."}
            )
        return attrs


class VerifyRegisterCompleteSerializer(serializers.Serializer):
    phone = serializers.CharField(max_length=22)
    otp_code = serializers.CharField(max_length=6)

    def validate_phone(self, value):
        try:
            return normalize_phone_registration(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc


class VerifyLoginSerializer(serializers.Serializer):
    phone = serializers.CharField(required=False, allow_blank=True, max_length=22)
    email = serializers.EmailField(required=False, allow_blank=True)
    otp_code = serializers.CharField(max_length=6)

    def validate(self, attrs):
        phone_raw = attrs.get("phone") or ""
        phone_raw = phone_raw.strip() if isinstance(phone_raw, str) else ""
        email = attrs.get("email")
        email = (
            email.strip().lower()
            if isinstance(email, str) and email.strip()
            else None
        )

        phone = None
        if phone_raw:
            try:
                phone = normalize_phone_registration(phone_raw)
            except ValueError as exc:
                raise serializers.ValidationError(str(exc)) from exc

        if not phone and not email:
            raise serializers.ValidationError("Provide phone or email")
        attrs["phone"] = phone
        attrs["email"] = email
        return attrs


class AdminLoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField()


class ProfileUpdateSerializer(serializers.Serializer):
    full_name = serializers.CharField(required=False, allow_blank=False, max_length=255)
    email = serializers.EmailField(required=False, allow_blank=False)
    date_of_birth = serializers.CharField(required=False, allow_blank=False)
    gender = serializers.CharField(required=False, allow_blank=False, max_length=32)
    address = serializers.CharField(required=False, allow_blank=False)
    city = serializers.CharField(required=False, allow_blank=False, max_length=128)
    pin_code = serializers.CharField(required=False, allow_blank=False, max_length=16)
    state = serializers.CharField(required=False, allow_blank=False, max_length=128)
    country = serializers.CharField(required=False, allow_blank=False, max_length=128)
    payout_preference = serializers.ChoiceField(
        required=False, choices=["BANK", "UPI"]
    )

    def validate_full_name(self, value: str) -> str:
        return value.strip()

    def validate_email(self, value: str) -> str:
        email = value.strip().lower()
        user = self.context.get("user")
        qs = User.objects.filter(email=email)
        if user:
            qs = qs.exclude(pk=user.pk)
        if qs.exists():
            raise serializers.ValidationError("Email already exists.")
        return email

    def validate_date_of_birth(self, value: str):
        raw = value.strip()
        for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
        raise serializers.ValidationError("Use DD/MM/YYYY or YYYY-MM-DD.")

    def validate_gender(self, value: str) -> str:
        raw = (value or "").strip().lower()
        mapping = {
            "m": "M",
            "male": "M",
            "f": "F",
            "female": "F",
            "o": "O",
            "other": "O",
            "u": "U",
            "undisclosed": "U",
            "prefer_not_to_say": "U",
            "prefer not to say": "U",
        }
        normalized = mapping.get(raw)
        if not normalized:
            raise serializers.ValidationError(
                "Gender must be one of: Male, Female, Other, Prefer not to say (or M/F/O/U)."
            )
        return normalized

    def validate_address(self, value: str) -> str:
        return value.strip()

    def validate_city(self, value: str) -> str:
        return value.strip()

    def validate_pin_code(self, value: str) -> str:
        return value.strip()

    def validate_state(self, value: str) -> str:
        return value.strip()

    def validate_country(self, value: str) -> str:
        return value.strip()
