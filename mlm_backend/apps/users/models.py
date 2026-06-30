from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models

from .managers import UserManager


class User(AbstractBaseUser, PermissionsMixin):
    class KYCStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        VERIFIED = "VERIFIED", "Verified"
        REJECTED = "REJECTED", "Rejected"

    class Role(models.TextChoices):
        SUPER_ADMIN = "SUPER_ADMIN", "Super Admin"
        FINANCE = "FINANCE", "Finance"
        SUPPORT = "SUPPORT", "Support"
        MEMBER = "MEMBER", "Member"

    class AccountStatus(models.TextChoices):
        INACTIVE = "INACTIVE", "Inactive"
        ACTIVE = "ACTIVE", "Active"
        CAPPED = "CAPPED", "Capped"
        SUSPENDED = "SUSPENDED", "Suspended"
        DEACTIVATED = "DEACTIVATED", "Deactivated"

    class PayoutPreference(models.TextChoices):
        BANK = "BANK", "Bank"
        UPI = "UPI", "UPI"

    phone = models.CharField(max_length=20, unique=True, null=True, blank=True)
    email = models.EmailField(unique=True, null=True, blank=True)
    login_identifier = models.CharField(max_length=255, unique=True, db_index=True)
    full_name = models.CharField(max_length=255)

    pan_number = models.CharField(max_length=10, null=True, blank=True)
    aadhaar_number = models.CharField(max_length=12, null=True, blank=True)
    kyc_status = models.CharField(
        max_length=20, choices=KYCStatus.choices, default=KYCStatus.PENDING
    )
    kyc_submitted_at = models.DateTimeField(null=True, blank=True)
    kyc_reviewed_at = models.DateTimeField(null=True, blank=True)
    kyc_rejection_reason = models.TextField(blank=True, default="")
    kyc_invitation_sent_at = models.DateTimeField(null=True, blank=True)
    kyc_first_approved_at = models.DateTimeField(null=True, blank=True)
    compliance_submission_version = models.PositiveIntegerField(default=0)

    is_member = models.BooleanField(default=False)
    role = models.CharField(
        max_length=20, choices=Role.choices, default=Role.MEMBER
    )

    member_id = models.CharField(max_length=32, unique=True)
    referral_code = models.CharField(max_length=16, unique=True)
    referral_link = models.URLField(max_length=500)
    sponsor = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="direct_referrals",
    )

    account_status = models.CharField(
        max_length=20,
        choices=AccountStatus.choices,
        default=AccountStatus.INACTIVE,
    )

    bank_account_number = models.CharField(max_length=64, null=True, blank=True)
    bank_ifsc = models.CharField(max_length=20, null=True, blank=True)
    bank_name = models.CharField(max_length=255, null=True, blank=True)
    upi_id = models.CharField(max_length=100, null=True, blank=True)
    payout_preference = models.CharField(
        max_length=10,
        choices=PayoutPreference.choices,
        default=PayoutPreference.UPI,
    )

    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    otp_locked_until = models.DateTimeField(null=True, blank=True)
    direct_referral_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = "login_identifier"
    REQUIRED_FIELDS: list[str] = []

    objects = UserManager()

    class Meta:
        db_table = "users_user"
        constraints = [
            models.UniqueConstraint(
                fields=["pan_number"],
                condition=models.Q(pan_number__isnull=False) & ~models.Q(pan_number=""),
                name="uniq_user_pan_number",
            ),
            models.UniqueConstraint(
                fields=["aadhaar_number"],
                condition=models.Q(aadhaar_number__isnull=False)
                & ~models.Q(aadhaar_number=""),
                name="uniq_user_aadhaar_number",
            ),
        ]

    def __str__(self):
        return f"{self.member_id} ({self.full_name})"

    def save(self, *args, **kwargs):
        if self.phone:
            self.phone = self.phone.strip()
        if self.email:
            self.email = self.email.strip().lower()
        if self.phone:
            self.login_identifier = self.phone.strip()
        elif self.email:
            self.login_identifier = self.email
        elif self.login_identifier and "@" in self.login_identifier:
            # Superuser created with email as login_identifier but email field empty
            self.login_identifier = self.login_identifier.strip().lower()
        super().save(*args, **kwargs)


class AccountDeletionRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        COMPLETED = "COMPLETED", "Completed"

    user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="account_deletion_requests",
    )
    snapshot_member_id = models.CharField(max_length=32)
    snapshot_full_name = models.CharField(max_length=255)
    snapshot_email = models.EmailField(null=True, blank=True)
    snapshot_phone = models.CharField(max_length=20, null=True, blank=True)
    reason = models.TextField()
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="account_deletion_requests_completed",
    )

    class Meta:
        db_table = "users_account_deletion_request"
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(status="PENDING") & models.Q(user__isnull=False),
                name="uniq_pending_account_deletion_per_user",
            ),
        ]

    def __str__(self):
        return f"{self.snapshot_member_id} ({self.status})"
