import re
from rest_framework import serializers

from apps.agreements.identity_uniqueness import validate_identity_uniqueness_for_user
from apps.agreements.models import AgreementCategory, LegalDocument, MemberComplianceProfile
from apps.common.phone_utils import normalize_phone_registration
from apps.common.url_utils import public_media_url


PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
AADHAR_RE = re.compile(r"^\d{12}$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")


class LegalDocumentPublicSerializer(serializers.ModelSerializer):
    pdf_file_url = serializers.SerializerMethodField()
    is_agreement_accepted = serializers.SerializerMethodField()

    class Meta:
        model = LegalDocument
        fields = [
            "id",
            "name",
            "category",
            "document_type",
            "year",
            "description",
            "content_html",
            "version",
            "pdf_url",
            "pdf_file_url",
            "requires_acceptance_for_compliance",
            "is_agreement_accepted",
        ]

    def get_pdf_file_url(self, obj):
        req = self.context.get("request")
        if obj.pdf_file and req:
            return public_media_url(req, obj.pdf_file) or obj.pdf_file.url
        return None

    def get_is_agreement_accepted(self, obj) -> bool:
        accepted_versions = self.context.get("accepted_versions") or {}
        accepted = accepted_versions.get(obj.id)
        return bool(accepted and accepted == obj.version)


class LegalDocumentAdminSerializer(serializers.ModelSerializer):
    class Meta:
        model = LegalDocument
        fields = "__all__"
        read_only_fields = ("created_at", "updated_at")

    def validate_category(self, value):
        if value is None:
            return value
        raw = (value or "").strip()
        if not raw:
            raise serializers.ValidationError("Category cannot be empty.")
        for choice in AgreementCategory.values:
            if raw.casefold() == choice.casefold():
                return choice
        allowed = ", ".join(AgreementCategory.values)
        raise serializers.ValidationError(
            f"Category must be one of: {allowed}."
        )

    def to_representation(self, instance):
        data = super().to_representation(instance)
        req = self.context.get("request")
        pdf = getattr(instance, "pdf_file", None)
        if pdf:
            try:
                data["pdf_file"] = public_media_url(req, pdf) or pdf.url
            except Exception:
                data["pdf_file"] = str(pdf)
        else:
            data["pdf_file"] = None
        return data

    def validate(self, attrs):
        """Allow HTML-only, or uploaded PDF file."""
        inst = self.instance
        if inst is None:
            cat = attrs.get("category", "")
            if not (cat or "").strip():
                raise serializers.ValidationError(
                    {"category": "This field is required."}
                )
        pdf_file = attrs.get("pdf_file", inst.pdf_file if inst else None)
        content = attrs.get("content_html", (inst.content_html if inst else "") or "")
        if not (content or pdf_file):
            raise serializers.ValidationError(
                "Provide at least one of: content_html or pdf_file."
            )
        # Keep payload consistent when file upload is the source of truth.
        if pdf_file and "pdf_url" in attrs:
            attrs["pdf_url"] = ""
        return attrs


class ComplianceRequiredAgreementAdminSerializer(LegalDocumentAdminSerializer):
    """
    Admin serializer for the singleton compliance-required legal agreement.
    Forces category LEGAL DOCUMENT and requires_acceptance_for_compliance=True.
    """

    class Meta(LegalDocumentAdminSerializer.Meta):
        read_only_fields = LegalDocumentAdminSerializer.Meta.read_only_fields + (
            "category",
            "requires_acceptance_for_compliance",
        )

    def validate(self, attrs):
        attrs = dict(attrs)
        attrs.setdefault("category", AgreementCategory.LEGAL_DOCUMENT)
        attrs.setdefault("requires_acceptance_for_compliance", True)
        return super().validate(attrs)

    def create(self, validated_data):
        validated_data["category"] = AgreementCategory.LEGAL_DOCUMENT
        validated_data["requires_acceptance_for_compliance"] = True
        validated_data.setdefault("is_active", True)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        instance = super().update(instance, validated_data)
        update_fields: list[str] = []
        if instance.category != AgreementCategory.LEGAL_DOCUMENT:
            instance.category = AgreementCategory.LEGAL_DOCUMENT
            update_fields.append("category")
        if not instance.requires_acceptance_for_compliance:
            instance.requires_acceptance_for_compliance = True
            update_fields.append("requires_acceptance_for_compliance")
        if update_fields:
            update_fields.append("updated_at")
            instance.save(update_fields=update_fields)
        return instance


class AgreementOTPSendSerializer(serializers.Serializer):
    document_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
    )
    declaration = serializers.CharField(
        min_length=1,
        max_length=4000,
        trim_whitespace=True,
    )


class AgreementOTPVerifySerializer(serializers.Serializer):
    document_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
    )
    otp_code = serializers.CharField(max_length=12)


class ComplianceSubmitSerializer(serializers.Serializer):
    date_of_birth = serializers.DateField(input_formats=["%d/%m/%Y"])
    gender = serializers.CharField(max_length=32)

    full_address = serializers.CharField(min_length=1, max_length=2000)
    city = serializers.CharField(max_length=128)
    pin_code = serializers.CharField(max_length=16)
    state = serializers.CharField(max_length=128)
    country = serializers.CharField(max_length=128)

    pan_number = serializers.CharField(
        max_length=10, required=False, allow_blank=True, default=""
    )
    name_on_pan = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )
    aadhar_number = serializers.CharField(max_length=14)
    name_on_aadhar = serializers.CharField(max_length=255)

    nominee_name = serializers.CharField(max_length=255)
    nominee_relationship = serializers.CharField(max_length=128)
    nominee_phone = serializers.CharField(max_length=22)
    nominee_date_of_birth = serializers.DateField(input_formats=["%d/%m/%Y"])

    account_holder_name = serializers.CharField(max_length=255)
    account_number = serializers.CharField(max_length=64)
    bank_name = serializers.CharField(max_length=255)
    ifsc = serializers.CharField(max_length=20)
    branch = serializers.CharField(max_length=255)
    account_type = serializers.ChoiceField(
        choices=MemberComplianceProfile.BankAccountType.choices,
    )
    payout_preference = serializers.CharField(
        max_length=10,
        required=False,
        allow_blank=True,
        default="",
    )
    upi_id = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default=""
    )

    def validate_pan_number(self, value: str) -> str:
        u = (value or "").strip().upper()
        if not u:
            return ""
        if not PAN_RE.match(u):
            raise serializers.ValidationError("Invalid PAN format.")
        return u

    def validate_aadhar_number(self, value: str) -> str:
        d = "".join(c for c in value if c.isdigit())
        if not AADHAR_RE.match(d):
            raise serializers.ValidationError("Aadhaar must be 12 digits.")
        return d

    def validate_ifsc(self, value: str) -> str:
        u = value.strip().upper()
        if not IFSC_RE.match(u):
            raise serializers.ValidationError("Invalid IFSC format.")
        return u

    def validate_nominee_phone(self, value: str) -> str:
        try:
            return normalize_phone_registration(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate_gender(self, value: str) -> str:
        raw = (value or "").strip().lower()
        mapping = {
            "m": "M",
            "male": "M",
            "f": "F",
            "female": "F",
            "o": "O",
            "other": "O",
            "u": "U",
            "undisclosed": "U",
            "prefer_not_to_say": "U",
            "prefer not to say": "U",
        }
        normalized = mapping.get(raw)
        if not normalized:
            raise serializers.ValidationError(
                "Gender must be one of: Male, Female, Other, Prefer not to say (or M/F/O/U)."
            )
        return normalized

    def validate_payout_preference(self, value: str) -> str:
        raw = (value or "").strip().upper()
        if not raw:
            return "UPI"
        if raw not in ("BANK", "UPI"):
            raise serializers.ValidationError("Must be BANK or UPI.")
        return raw

    def validate(self, attrs):
        user = self.context.get("user")
        if user is None:
            return attrs
        errors = validate_identity_uniqueness_for_user(
            pan=attrs.get("pan_number"),
            aadhaar=attrs.get("aadhar_number"),
            user_id=user.pk,
        )
        if errors:
            raise serializers.ValidationError(errors)
        return attrs
