from django.conf import settings
from django.db import models


class Order(models.Model):
    class Status(models.TextChoices):
        CREATED = "CREATED", "Created"
        PAID = "PAID", "Paid"
        FAILED = "FAILED", "Failed"
        REFUNDED = "REFUNDED", "Refunded"

    class PlacementStatus(models.TextChoices):
        PENDING = "PENDING", "Pending manual or auto placement"
        PLACED_MANUAL = "PLACED_MANUAL", "Placed by sponsor"
        PLACED_AUTO = "PLACED_AUTO", "Placed by auto job"
        PLACED_ADMIN = "PLACED_ADMIN", "Placed by admin"
        FAILED = "FAILED", "Placement failed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="orders",
    )
    ebook = models.ForeignKey(
        "courses.EBook",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="orders",
    )
    order_number = models.CharField(max_length=40, unique=True)
    base_price = models.DecimalField(max_digits=10, decimal_places=2, default=200)
    gst_amount = models.DecimalField(max_digits=10, decimal_places=2, default=36)
    gateway_charge = models.DecimalField(max_digits=10, decimal_places=2, default=5.72)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=241.72)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2)
    sponsor_code_used = models.ForeignKey(
        "sponsor_slots.SponsorSlotCode",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="orders_using_code",
    )
    is_sponsor_slot_redemption = models.BooleanField(default=False)
    is_retail_purchase = models.BooleanField(default=False)
    razorpay_order_id = models.CharField(max_length=64, null=True, blank=True)
    razorpay_payment_id = models.CharField(max_length=64, null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.CREATED)
    gst_invoice_number = models.CharField(max_length=40, null=True, blank=True)
    refund_eligible_until = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    placement_deadline_at = models.DateTimeField(null=True, blank=True)
    placement_status = models.CharField(
        max_length=32,
        choices=PlacementStatus.choices,
        null=True,
        blank=True,
    )
    placement_leg_requested = models.CharField(max_length=10, null=True, blank=True)
    placement_resolved_at = models.DateTimeField(null=True, blank=True)
    placement_failure_reason = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "payments_order"


class GSTInvoice(models.Model):
    order = models.OneToOneField(
        Order,
        on_delete=models.CASCADE,
        related_name="gst_invoice",
    )
    invoice_number = models.CharField(max_length=40, unique=True)
    hsn_sac_code = models.CharField(max_length=10, default="9992")
    base_amount = models.DecimalField(max_digits=10, decimal_places=2)
    cgst = models.DecimalField(max_digits=10, decimal_places=2)
    sgst = models.DecimalField(max_digits=10, decimal_places=2)
    total_gst = models.DecimalField(max_digits=10, decimal_places=2)
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    grand_total = models.DecimalField(max_digits=10, decimal_places=2)
    pdf_url = models.URLField(max_length=500, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "payments_gst_invoice"
