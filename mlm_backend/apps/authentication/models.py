from django.db import models


class OTPRecord(models.Model):
    class Purpose(models.TextChoices):
        REGISTER = "REGISTER", "Register"
        LOGIN = "LOGIN", "Login"
        KYC = "KYC", "KYC"
        ADMIN_LOGIN = "ADMIN_LOGIN", "Admin Login"

    phone = models.CharField(max_length=15, null=True, blank=True)
    email = models.EmailField(null=True, blank=True)
    otp_code = models.CharField(max_length=6)
    purpose = models.CharField(max_length=20, choices=Purpose.choices)
    is_used = models.BooleanField(default=False)
    attempts = models.PositiveSmallIntegerField(default=0)
    expires_at = models.DateTimeField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    device_fingerprint = models.CharField(max_length=128, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "auth_otp_record"
        indexes = [
            models.Index(fields=["phone", "purpose", "created_at"]),
            models.Index(fields=["email", "purpose", "created_at"]),
        ]
