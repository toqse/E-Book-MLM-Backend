from django.conf import settings
from django.db import models


class TdsLedger(models.Model):
    """
    Per-user, per-financial-year cumulative ledger for Section 194H TDS.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tds_ledgers",
    )
    financial_year = models.CharField(max_length=7)  # e.g. "2025-26"
    total_earned = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_tds = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tds_triggered = models.BooleanField(default=False)
    tds_triggered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "tds_ledger"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "financial_year"], name="unique_user_fy"
            )
        ]

