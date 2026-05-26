from django.conf import settings
from django.db import models


class Wallet(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="wallet",
    )
    cash_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_earned = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_withdrawn = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_tds_deducted = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    current_band = models.PositiveSmallIntegerField(default=1)
    band_cash_withdrawn_fy = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    fy_label = models.CharField(max_length=9, default="2026-27")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wallet_wallet"


class WalletTransaction(models.Model):
    class TxType(models.TextChoices):
        CREDIT = "CREDIT", "Credit"
        DEBIT = "DEBIT", "Debit"
        ADJUSTMENT = "ADJUSTMENT", "Adjustment"
        TDS = "TDS", "TDS Withheld"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="wallet_transactions",
    )
    tx_type = models.CharField(max_length=20, choices=TxType.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    balance_after = models.DecimalField(max_digits=12, decimal_places=2)
    reference = models.CharField(max_length=64, blank=True)
    meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "wallet_transaction"
        ordering = ["-created_at"]


class WithdrawalRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        PROCESSING = "PROCESSING", "Processing"
        PAID = "PAID", "Paid"
        REJECTED = "REJECTED", "Rejected"
        FAILED = "FAILED", "Failed"

    class PayoutMethod(models.TextChoices):
        BANK = "BANK", "Bank"
        UPI = "UPI", "UPI"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="withdrawal_requests",
    )
    band = models.PositiveSmallIntegerField()
    amount_requested = models.DecimalField(max_digits=12, decimal_places=2)
    tds_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    net_payable = models.DecimalField(max_digits=12, decimal_places=2)
    tds_section = models.CharField(max_length=10, null=True, blank=True)
    payout_method = models.CharField(
        max_length=10,
        choices=PayoutMethod.choices,
        default=PayoutMethod.UPI,
    )
    payout_destination_hint = models.CharField(max_length=120, blank=True, default="")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    razorpay_payout_id = models.CharField(max_length=64, null=True, blank=True)
    utr_number = models.CharField(max_length=64, null=True, blank=True)
    reject_reason = models.TextField(blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="approved_withdrawals",
    )
    paid_at = models.DateTimeField(null=True, blank=True)
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="paid_withdrawals",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wallet_withdrawal"
