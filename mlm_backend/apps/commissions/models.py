from django.conf import settings
from django.db import models


class CommissionLedger(models.Model):
    class CommissionType(models.TextChoices):
        DIRECT = "DIRECT", "Direct"
        UPLINE_L2 = "UPLINE_L2", "Upline L2"
        UPLINE_L3 = "UPLINE_L3", "Upline L3"
        UPLINE_L4 = "UPLINE_L4", "Upline L4"
        MILESTONE = "MILESTONE", "Milestone"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        CREDITED = "CREDITED", "Credited"
        REVERSED = "REVERSED", "Reversed"
        HELD = "HELD", "Held"

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="commissions_received",
    )
    source_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="commissions_generated",
    )
    order = models.ForeignKey(
        "payments.Order",
        on_delete=models.CASCADE,
        related_name="commission_entries",
    )
    commission_type = models.CharField(max_length=20, choices=CommissionType.choices)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    tds_deducted = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    net_amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "commissions_ledger"


class MilestoneRecord(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="milestone_records",
    )
    milestone_referrals = models.PositiveIntegerField()
    bonus_amount = models.DecimalField(max_digits=10, decimal_places=2)
    tds_deducted = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    net_bonus = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, default="PENDING")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "commissions_milestone"
