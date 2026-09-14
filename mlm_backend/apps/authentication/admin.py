from django.contrib import admin

from .models import DemoOtpAllowlist, OTPRecord, StoreReferralLead


@admin.register(DemoOtpAllowlist)
class DemoOtpAllowlistAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "phone",
        "email",
        "demo_otp_code",
        "is_active",
        "note",
        "updated_at",
    )
    list_filter = ("is_active",)
    search_fields = ("phone", "email", "note")
    ordering = ("-updated_at",)
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "phone",
                    "email",
                    "demo_otp_code",
                    "is_active",
                    "note",
                ),
                "description": (
                    "Allowlisted phone (E.164 with country code) and/or email may use this "
                    "fixed Demo OTP on register, login, KYC, admin, and agreement verify — "
                    "even when development mode is off. Clients must still request an OTP first."
                ),
            },
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )


@admin.register(OTPRecord)
class OTPRecordAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "phone",
        "email",
        "purpose",
        "otp_code",
        "is_used",
        "attempts",
        "expires_at",
        "created_at",
    )
    list_filter = ("purpose", "is_used", "created_at")
    search_fields = ("phone", "email", "otp_code", "ip_address")
    readonly_fields = ("created_at",)
    autocomplete_fields = ("registration_sponsor",)
    ordering = ("-created_at",)


@admin.register(StoreReferralLead)
class StoreReferralLeadAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "phone",
        "referral_code",
        "platform",
        "expires_at",
        "created_at",
        "updated_at",
    )
    list_filter = ("platform", "expires_at")
    search_fields = ("phone", "referral_code")
    readonly_fields = ("created_at", "updated_at")
    ordering = ("-created_at",)
