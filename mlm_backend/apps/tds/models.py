from django.conf import settings
from django.db import models


class TdsLedger(models.Model):
    """
    Per-user, per-financial-year, per-section cumulative TDS ledger.

    Section 194H tracks commission income (cash bands).
    Section 194R tracks perquisite/benefit income (slot bands).
    Each section has its own ₹20,000 FY threshold and independent counters.
    """

    SECTION_194H = "194H"
    SECTION_194R = "194R"
    SECTION_CHOICES = [
        (SECTION_194H, "194H"),
        (SECTION_194R, "194R"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tds_ledgers",
    )
    financial_year = models.CharField(max_length=7)  # e.g. "2025-26"
    section = models.CharField(
        max_length=10,
        choices=SECTION_CHOICES,
        default=SECTION_194H,
    )
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
                fields=["user", "financial_year", "section"],
                name="unique_user_fy_section",
            )
        ]

