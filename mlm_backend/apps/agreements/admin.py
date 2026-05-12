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
    list_display = ("user", "pan_number", "updated_at")
