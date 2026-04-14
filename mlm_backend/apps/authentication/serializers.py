from rest_framework import serializers


class SendOTPSerializer(serializers.Serializer):
    phone = serializers.CharField(required=False, allow_blank=True, max_length=15)
    email = serializers.EmailField(required=False, allow_blank=True)
    purpose = serializers.ChoiceField(
        choices=["REGISTER", "LOGIN", "KYC", "ADMIN_LOGIN"], default="LOGIN"
    )

    def validate(self, attrs):
        if not attrs.get("phone") and not attrs.get("email"):
            raise serializers.ValidationError("Provide phone or email")
        return attrs


class VerifyRegisterSerializer(serializers.Serializer):
    phone = serializers.CharField(required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    otp_code = serializers.CharField(max_length=6)
    full_name = serializers.CharField(max_length=255)
    referral_code = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        if not attrs.get("phone") and not attrs.get("email"):
            raise serializers.ValidationError("Provide phone or email")
        return attrs


class VerifyLoginSerializer(serializers.Serializer):
    phone = serializers.CharField(required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    otp_code = serializers.CharField(max_length=6)

    def validate(self, attrs):
        if not attrs.get("phone") and not attrs.get("email"):
            raise serializers.ValidationError("Provide phone or email")
        return attrs


class KYCSubmitSerializer(serializers.Serializer):
    pan_number = serializers.CharField(max_length=10)
    aadhaar_number = serializers.CharField(max_length=12)


class BankSerializer(serializers.Serializer):
    bank_account_number = serializers.CharField(required=False, allow_blank=True)
    bank_ifsc = serializers.CharField(required=False, allow_blank=True)
    upi_id = serializers.CharField(required=False, allow_blank=True)
    payout_preference = serializers.ChoiceField(choices=["BANK", "UPI"], default="UPI")


class AdminLoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField()
