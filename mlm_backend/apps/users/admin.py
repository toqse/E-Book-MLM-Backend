from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("member_id", "login_identifier", "full_name", "role", "is_member", "account_status")
    search_fields = ("member_id", "phone", "email", "referral_code")
    ordering = ("member_id",)
    readonly_fields = ("created_at", "updated_at", "last_login")

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "login_identifier",
                    "password",
                )
            },
        ),
        (
            "Personal info",
            {
                "fields": (
                    "full_name",
                    "phone",
                    "email",
                    "pan_number",
                    "aadhaar_number",
                )
            },
        ),
        (
            "MLM profile",
            {
                "fields": (
                    "member_id",
                    "referral_code",
                    "referral_link",
                    "sponsor",
                    "role",
                    "is_member",
                    "account_status",
                    "direct_referral_count",
                )
            },
        ),
        (
            "KYC and payouts",
            {
                "fields": (
                    "kyc_status",
                    "kyc_submitted_at",
                    "kyc_reviewed_at",
                    "kyc_rejection_reason",
                    "compliance_submission_version",
                    "bank_account_number",
                    "bank_ifsc",
                    "bank_name",
                    "upi_id",
                    "payout_preference",
                    "otp_locked_until",
                )
            },
        ),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        ("Important dates", {"fields": ("last_login", "created_at", "updated_at")}),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "login_identifier",
                    "full_name",
                    "phone",
                    "email",
                    "member_id",
                    "referral_code",
                    "referral_link",
                    "role",
                    "is_member",
                    "is_staff",
                    "is_active",
                    "password1",
                    "password2",
                ),
            },
        ),
    )
