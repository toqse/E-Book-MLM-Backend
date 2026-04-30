from rest_framework import serializers

from apps.common.phone_utils import normalize_phone_registration


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
