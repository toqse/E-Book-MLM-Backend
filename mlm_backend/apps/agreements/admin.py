from django.contrib import admin

from .models import (
    LegalDocument,
    MemberComplianceProfile,
    UserAgreementAcceptance,
    UserAgreementAcceptanceDeclaration,
    UserAgreementAcceptanceProof,
)


@admin.register(LegalDocument)
class LegalDocumentAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "category", "version", "is_active")
    search_fields = ("name",)
    list_filter = ("is_active", "requires_acceptance_for_compliance")


@admin.register(UserAgreementAcceptance)
class UserAgreementAcceptanceAdmin(admin.ModelAdmin):
    list_display = ("user", "document", "version_accepted", "accepted_at")
    readonly_fields = ("accepted_at",)


@admin.register(UserAgreementAcceptanceDeclaration)
class UserAgreementAcceptanceDeclarationAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "acceptance_batch_id", "created_at")
    readonly_fields = ("created_at",)


@admin.register(UserAgreementAcceptanceProof)
class UserAgreementAcceptanceProofAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "acceptance_batch_id", "issued_at", "created_at")
    readonly_fields = ("created_at", "signature_hex", "issued_at")


@admin.register(MemberComplianceProfile)
class MemberComplianceProfileAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "kyc_status",
        "pan_number",
        "aadhar_number_masked",
        "payout_preference",
        "updated_at",
    )
    list_select_related = ("user",)
    list_filter = ("payout_preference", "account_type", "user__kyc_status")
    search_fields = (
        "user__member_id",
        "user__full_name",
        "user__phone",
        "user__email",
        "pan_number",
        "aadhar_number",
        "account_number",
    )
    readonly_fields = ("updated_at",)
    fieldsets = (
        (None, {"fields": ("user",)}),
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

    @admin.display(description="KYC status", ordering="user__kyc_status")
    def kyc_status(self, obj):
        return obj.user.kyc_status

    @admin.display(description="Aadhaar")
    def aadhar_number_masked(self, obj):
        n = (obj.aadhar_number or "").strip()
        if len(n) < 4:
            return n or "-"
        return f"XXXX-XXXX-{n[-4:]}"
