from django.conf import settings
from django.db import models


class AgreementCategory(models.TextChoices):
    """Allowed agreement categories (admin create/update)."""

    LEGAL_DOCUMENT = "LEGAL DOCUMENT", "Legal document"
    KYC_IDENTITY = "KYC & IDENTITY", "KYC & Identity"
    PURCHASE_TAX = "PURCHASE & TAX", "Purchase & Tax"


class LegalDocument(models.Model):
    """Superadmin-managed legal / policy documents for member acceptance."""

    name = models.CharField(max_length=255)
    category = models.CharField(max_length=128, blank=True, default="")
    document_type = models.CharField(max_length=128, blank=True, default="")
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    description = models.TextField(blank=True, default="")
    content_html = models.TextField(blank=True, default="")
    version = models.CharField(max_length=64, default="1.0")
    pdf_url = models.URLField(max_length=500, blank=True, default="")
    pdf_file = models.FileField(
        upload_to="legal_documents/%Y/%m/",
        blank=True,
        null=True,
    )
    is_active = models.BooleanField(default=True)
    requires_acceptance_for_compliance = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "agreements_legal_document"
        ordering = ["category", "name", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["requires_acceptance_for_compliance"],
                condition=models.Q(
                    requires_acceptance_for_compliance=True,
                    is_active=True,
                ),
                name="uniq_active_compliance_required_legal_doc",
            ),
        ]

    def __str__(self):
        return f"{self.name} v{self.version}"


class UserAgreementAcceptance(models.Model):
    """Audit trail of OTP-verified acceptances."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="agreement_acceptances",
    )
    document = models.ForeignKey(
        LegalDocument,
        on_delete=models.CASCADE,
        related_name="acceptances",
    )
    version_accepted = models.CharField(max_length=64)
    acceptance_batch_id = models.UUIDField()
    accepted_at = models.DateTimeField(auto_now_add=True)
    accepted_ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        db_table = "agreements_user_acceptance"
        indexes = [
            models.Index(fields=["user", "document", "-accepted_at"]),
        ]


class UserAgreementAcceptanceProof(models.Model):
    """
    One PDF per OTP acceptance batch: human-readable summary + HMAC over canonical payload.
    Served for download via API (Content-Disposition: attachment).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="agreement_acceptance_proofs",
    )
    acceptance_batch_id = models.UUIDField()
    signature_hex = models.CharField(max_length=64)
    issued_at = models.DateTimeField()
    pdf_file = models.FileField(
        upload_to="agreement_proofs/%Y/%m/",
        max_length=500,
        blank=True,
        null=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "agreements_user_acceptance_proof"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "acceptance_batch_id"],
                name="uniq_agreement_acceptance_proof_user_batch",
            ),
        ]


class UserAgreementAcceptanceDeclaration(models.Model):
    """Member declaration text captured at OTP send and tied to an acceptance batch at verify."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="agreement_acceptance_declarations",
    )
    acceptance_batch_id = models.UUIDField()
    declaration_text = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "agreements_user_acceptance_declaration"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "acceptance_batch_id"],
                name="uniq_agreement_declaration_user_batch",
            ),
        ]


class MemberComplianceProfile(models.Model):
    """Extended KYC / bank / nominee data; payouts still read synced User columns."""

    class Gender(models.TextChoices):
        M = "M", "Male"
        F = "F", "Female"
        O = "O", "Other"
        UNDISCLOSED = "U", "Prefer not to say"

    class BankAccountType(models.TextChoices):
        SAVINGS = "SAVINGS", "Savings"
        CURRENT = "CURRENT", "Current"
        NRO = "NRO", "NRO"
        NRE = "NRE", "NRE"
        RECURRING = "RECURRING", "Recurring deposit"
        OTHER = "OTHER", "Other"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="compliance_profile",
    )
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(
        max_length=1,
        choices=Gender.choices,
        blank=True,
        default="",
    )
    full_address = models.TextField(blank=True, default="")
    city = models.CharField(max_length=128, blank=True, default="")
    pin_code = models.CharField(max_length=16, blank=True, default="")
    state = models.CharField(max_length=128, blank=True, default="")
    country = models.CharField(max_length=128, blank=True, default="")

    pan_number = models.CharField(max_length=10, blank=True, default="")
    name_on_pan = models.CharField(max_length=255, blank=True, default="")
    aadhar_number = models.CharField(max_length=12, blank=True, default="")
    name_on_aadhar = models.CharField(max_length=255, blank=True, default="")
    pan_document = models.FileField(
        upload_to="kyc/pan/%Y/%m/",
        blank=True,
        null=True,
    )
    aadhar_document = models.FileField(
        upload_to="kyc/aadhaar/%Y/%m/",
        blank=True,
        null=True,
    )
    aadhar_front = models.FileField(
        upload_to="kyc/aadhaar/front/%Y/%m/",
        blank=True,
        null=True,
    )
    aadhar_back = models.FileField(
        upload_to="kyc/aadhaar/back/%Y/%m/",
        blank=True,
        null=True,
    )

    nominee_name = models.CharField(max_length=255, blank=True, default="")
    nominee_relationship = models.CharField(max_length=128, blank=True, default="")
    nominee_phone = models.CharField(max_length=22, blank=True, default="")
    nominee_date_of_birth = models.DateField(null=True, blank=True)

    account_holder_name = models.CharField(max_length=255, blank=True, default="")
    account_number = models.CharField(max_length=64, blank=True, default="")
    bank_name = models.CharField(max_length=255, blank=True, default="")
    ifsc = models.CharField(max_length=20, blank=True, default="")
    branch = models.CharField(max_length=255, blank=True, default="")
    account_type = models.CharField(
        max_length=20,
        choices=BankAccountType.choices,
        blank=True,
        default="",
    )
    payout_preference = models.CharField(
        max_length=10,
        choices=[("BANK", "Bank"), ("UPI", "UPI")],
        default="UPI",
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "agreements_member_compliance_profile"
