from django.conf import settings
from django.db import models


class SponsorSlotBatch(models.Model):
    issued_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sponsor_slot_batches",
    )
    band_number = models.PositiveSmallIntegerField()
    total_codes = models.PositiveSmallIntegerField(default=5)
    codes_redeemed = models.PositiveSmallIntegerField(default=0)
    codes_expired = models.PositiveSmallIntegerField(default=0)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "sponsor_slot_batch"


class SponsorSlotCode(models.Model):
    class Status(models.TextChoices):
        LOCKED = "LOCKED", "Locked"
        ACTIVE = "ACTIVE", "Active"
        REDEEMED = "REDEEMED", "Redeemed"
        EXPIRED = "EXPIRED", "Expired"
        SHARED = "SHARED", "Shared"

    class SharedVia(models.TextChoices):
        WHATSAPP = "WHATSAPP", "WhatsApp"
        SMS = "SMS", "SMS"
        EMAIL = "EMAIL", "Email"
        COPY = "COPY", "Copy"

    batch = models.ForeignKey(
        SponsorSlotBatch,
        on_delete=models.CASCADE,
        related_name="codes",
    )
    issued_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="issued_sponsor_codes",
    )
    code = models.CharField(max_length=32, unique=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    shared_via = models.CharField(
        max_length=20, choices=SharedVia.choices, null=True, blank=True
    )
    redeemed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="redeemed_sponsor_codes",
    )
    redeemed_order = models.ForeignKey(
        "payments.Order",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sponsor_redemptions",
    )
    unlock_at_total_earned = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Unlock this code once issuer wallet.total_earned reaches this amount.",
    )
    unlocked_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    unique_ips_attempted = models.JSONField(default=list, blank=True)
    is_flagged = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "sponsor_slot_code"
