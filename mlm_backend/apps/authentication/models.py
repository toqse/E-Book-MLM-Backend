from django.conf import settings
from django.db import models


class OTPRecord(models.Model):
    class Purpose(models.TextChoices):
        REGISTER = "REGISTER", "Register"
        LOGIN = "LOGIN", "Login"
        KYC = "KYC", "KYC"
        ADMIN_LOGIN = "ADMIN_LOGIN", "Admin Login"
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
