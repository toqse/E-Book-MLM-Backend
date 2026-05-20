from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from apps.agreements.models import MemberComplianceProfile

from .models import User


class MemberComplianceProfileInline(admin.StackedInline):
    model = MemberComplianceProfile
    can_delete = False
    extra = 0
    fk_name = "user"
    verbose_name_plural = "KYC compliance profile"
    fieldsets = (
        (
            "Personal & address",
            {
                "fields": (
                    "date_of_birth",
                    "gender",
                    "full_address",
                    "city",
                    "pin_code",
                    "state",
                    "country",
                )
            },
        ),
        (
            "Identity documents",
            {
                "fields": (
                    "pan_number",
                    "name_on_pan",
                    "pan_document",
                    "aadhar_number",
                    "name_on_aadhar",
                    "aadhar_front",
                    "aadhar_back",
                    "aadhar_document",
                )
            },
        ),
        (
            "Nominee",
            {
                "fields": (
                    "nominee_name",
                    "nominee_relationship",
                    "nominee_phone",
                    "nominee_date_of_birth",
                )
            },
        ),
        (
            "Bank / payout",
            {
                "fields": (
                    "account_holder_name",
                    "account_number",
                    "bank_name",
                    "ifsc",
                    "branch",
                    "account_type",
                    "payout_preference",
                )
            },
        ),
        ("Audit", {"fields": ("updated_at",)}),
    )
    readonly_fields = ("updated_at",)


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = (
        "member_id",
        "login_identifier",
        "full_name",
        "role",
        "is_member",
        "kyc_status",
        "account_status",
    )
    list_filter = (
        "kyc_status",
        "account_status",
        "role",
        "is_member",
        "is_staff",
        "is_active",
    )
    search_fields = ("member_id", "phone", "email", "referral_code", "full_name")
    ordering = ("member_id",)
    readonly_fields = ("created_at", "updated_at", "last_login")
    inlines = (MemberComplianceProfileInline,)

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
            "KYC and compliance",
            {
                "fields": (
                    "kyc_status",
                    "kyc_submitted_at",
                    "kyc_reviewed_at",
                    "kyc_rejection_reason",
                    "kyc_invitation_sent_at",
                    "compliance_submission_version",
                ),
                "description": (
                    "Use the KYC compliance profile section below to view or edit the "
                    "uploaded PAN/Aadhaar documents and bank/nominee details."
                ),
            },
        ),
        (
            "Payouts",
            {
                "fields": (
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
