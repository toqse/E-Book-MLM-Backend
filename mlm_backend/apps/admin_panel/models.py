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
    cooling_off_days = models.PositiveIntegerField(
        default=7,
        help_text="Days recent wallet credits are excluded from withdrawable balance.",
    )
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
    auto_process_milestone_bonuses = models.BooleanField(
        default=True,
        help_text="If true, milestone bonuses are credited automatically upon achievement; if false, they enter an admin queue for manual processing.",
    )
    milestone_bonus_overrides = models.JSONField(
        default=dict,
        blank=True,
        help_text="Optional mapping of milestone referral threshold -> bonus gross amount. Keys may be int or stringified int.",
    )

    razorpay_key_id = models.CharField(max_length=64, blank=True, default="")
    # Stored for admin configuration; do not expose back to clients.
    razorpay_key_secret = models.CharField(max_length=256, blank=True, default="")

    nodal_officer_name = models.CharField(max_length=120, blank=True, default="")
    nodal_officer_email = models.EmailField(blank=True, default="")
    nodal_officer_phone = models.CharField(max_length=32, blank=True, default="")
    grievance_sla_hours = models.PositiveIntegerField(default=48)
    refund_request_sla_hours = models.PositiveIntegerField(
        default=48,
        help_text="Hours from member request submission for open refund SLA (dashboard metrics).",
    )
    default_company_referral_code = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=(
            "When set, overrides DEFAULT_COMPANY_REFERRAL_CODE from the environment. "
            "Empty means use the env value."
        ),
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
