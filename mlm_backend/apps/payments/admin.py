from django.contrib import admin

from .models import GSTInvoice, Order


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "order_number",
        "user",
        "ebook",
        "status",
        "amount_paid",
        "razorpay_order_id",
        "razorpay_payment_id",
        "paid_at",
        "is_retail_purchase",
        "is_sponsor_slot_redemption",
        "created_at",
    )
    list_filter = (
        "status",
        "is_retail_purchase",
        "is_sponsor_slot_redemption",
        "created_at",
        "paid_at",
    )
    search_fields = (
        "order_number",
        "user__member_id",
        "user__full_name",
        "user__phone",
        "user__email",
        "razorpay_order_id",
        "razorpay_payment_id",
    )
    readonly_fields = ("created_at",)
    autocomplete_fields = ("user", "ebook", "sponsor_code_used")
    ordering = ("-id",)
    fieldsets = (
        (None, {"fields": ("user", "ebook", "order_number", "status")}),
        (
            "Amounts",
            {
                "fields": (
                    "base_price",
                    "gst_amount",
                    "gateway_charge",
                    "total_amount",
                    "discount_amount",
                    "amount_paid",
                )
            },
        ),
        (
            "Razorpay",
            {
                "fields": ("razorpay_order_id", "razorpay_payment_id"),
                "description": "Gateway order id (created at checkout) and payment id (set when paid).",
            },
        ),
        (
            "Flags & slot",
            {
                "fields": (
                    "is_retail_purchase",
                    "is_sponsor_slot_redemption",
                    "sponsor_code_used",
                )
            },
        ),
        (
            "Binary placement",
            {
                "fields": (
                    "placement_status",
                    "placement_deadline_at",
                    "placement_leg_requested",
                    "placement_resolved_at",
                    "placement_failure_reason",
                )
            },
        ),
        (
            "Invoicing & timeline",
            {"fields": ("gst_invoice_number", "paid_at", "refund_eligible_until", "created_at")},
        ),
    )


@admin.register(GSTInvoice)
class GSTInvoiceAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "invoice_number",
        "order",
        "base_amount",
        "total_gst",
        "grand_total",
        "created_at",
    )
    list_filter = ("created_at",)
    search_fields = ("invoice_number", "order__order_number", "order__user__member_id")
    readonly_fields = ("created_at",)
    autocomplete_fields = ("order",)
    ordering = ("-id",)
