from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class OTPRecord(models.Model):
    class Purpose(models.TextChoices):
        REGISTER = "REGISTER", "Register"
        LOGIN = "LOGIN", "Login"
        KYC = "KYC", "KYC"
        ADMIN_LOGIN = "ADMIN_LOGIN", "Admin Login"
        ADMIN_KYC = "ADMIN_KYC", "Admin KYC"
        AGREEMENT = "AGREEMENT", "Agreement"

    phone = models.CharField(max_length=20, null=True, blank=True)
    email = models.EmailField(null=True, blank=True)
    otp_code = models.CharField(max_length=6)
    payload = models.JSONField(default=dict, blank=True)
    purpose = models.CharField(max_length=20, choices=Purpose.choices)
    is_used = models.BooleanField(default=False)
    attempts = models.PositiveSmallIntegerField(default=0)
    expires_at = models.DateTimeField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    device_fingerprint = models.CharField(max_length=128, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    registration_full_name = models.CharField(max_length=255, blank=True, default="")
    registration_email = models.EmailField(null=True, blank=True)
    registration_referral_code = models.CharField(max_length=32, blank=True, default="")
    registration_sponsor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        db_table = "auth_otp_record"
        indexes = [
            models.Index(fields=["phone", "purpose", "created_at"]),
            models.Index(fields=["email", "purpose", "created_at"]),
        ]


class DemoOtpAllowlist(models.Model):
    """Fixed Demo OTP for allowlisted phone/email (e.g. Razorpay audit), even when development_mode is off."""

    phone = models.CharField(
        max_length=20,
        null=True,
        blank=True,
        unique=True,
        help_text="E.164 with country code, e.g. +919876543210",
    )
    email = models.EmailField(null=True, blank=True, unique=True)
    demo_otp_code = models.CharField(
        max_length=6,
        help_text="Fixed 6-digit OTP accepted for this phone/email on all OTP verify flows.",
    )
    is_active = models.BooleanField(default=True)
    note = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "auth_demo_otp_allowlist"
        verbose_name = "Demo OTP allowlist"
        verbose_name_plural = "Demo OTP allowlist"
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(phone__isnull=False) & ~models.Q(phone="")
                )
                | (models.Q(email__isnull=False) & ~models.Q(email="")),
                name="auth_demo_otp_phone_or_email",
            ),
        ]

    def clean(self):
        from apps.common.phone_utils import normalize_phone_registration

        phone = (self.phone or "").strip() or None
        email = (self.email or "").strip().lower() or None
        if not phone and not email:
            raise ValidationError("Provide at least a phone or an email.")
        if phone:
            try:
                phone = normalize_phone_registration(phone)
            except ValueError as exc:
                raise ValidationError({"phone": str(exc)}) from exc
        code = "".join(c for c in str(self.demo_otp_code or "").strip() if c.isdigit())
        if len(code) != 6:
            raise ValidationError({"demo_otp_code": "Demo OTP must be exactly 6 digits."})
        self.phone = phone
        self.email = email
        self.demo_otp_code = code

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        ident = self.phone or self.email or "?"
        status = "active" if self.is_active else "inactive"
        return f"{ident} ({status})"


class StoreReferralLead(models.Model):
    class Platform(models.TextChoices):
        ANDROID = "ANDROID", "Android"
        IOS = "IOS", "iOS"

    phone = models.CharField(max_length=20, unique=True)
    referral_code = models.CharField(max_length=32)
    platform = models.CharField(max_length=16, choices=Platform.choices)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "auth_store_referral_lead"
        indexes = [
            models.Index(fields=["expires_at"]),
        ]
