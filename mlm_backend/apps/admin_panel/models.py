from django.conf import settings
from django.db import models


class SystemConfig(models.Model):
    class AutoPlacementStrategy(models.TextChoices):
        LEFT_FIRST = "LEFT_FIRST", "Left first (default spillover)"
        RIGHT_FIRST = "RIGHT_FIRST", "Right first"
        LONG_LEG = "LONG_LEG", "Long leg (larger subtree first)"
        WEAK_LEG = "WEAK_LEG", "Weak leg (smaller subtree first)"

    product_base_price = models.DecimalField(max_digits=10, decimal_places=2, default=200)
    gst_rate = models.DecimalField(max_digits=6, decimal_places=4, default=0.1800)
    direct_commission = models.DecimalField(max_digits=10, decimal_places=2, default=30)
    upline_commission = models.DecimalField(max_digits=10, decimal_places=2, default=10)
    earning_cap = models.DecimalField(max_digits=12, decimal_places=2, default=22200)
    sponsor_slot_expiry_days = models.PositiveIntegerField(default=20)
    tds_194h_rate = models.DecimalField(max_digits=6, decimal_places=4, default=0.0200)
    tds_194r_rate = models.DecimalField(max_digits=6, decimal_places=4, default=0.1000)
    tds_cash_trigger = models.DecimalField(max_digits=12, decimal_places=2, default=20000)
    refund_window_days = models.PositiveIntegerField(default=7)
    placement_manual_window_hours = models.PositiveIntegerField(default=24)
    auto_placement_strategy = models.CharField(
        max_length=20,
        choices=AutoPlacementStrategy.choices,
        default=AutoPlacementStrategy.LEFT_FIRST,
    )
    is_repurchase_commission_allowed = models.BooleanField(
        default=False,
        help_text="If false, no MLM commissions on a buyer's 2nd+ paid non-retail orders.",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "admin_system_config"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)


class Grievance(models.Model):
    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        CLOSED = "CLOSED", "Closed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="grievances",
    )
    subject = models.CharField(max_length=255)
    body = models.TextField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    admin_response = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "admin_grievance"
