from django.contrib import admin

from .models import Grievance, SystemConfig


@admin.register(SystemConfig)
class SystemConfigAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "development_mode",
        "product_base_price",
        "earning_cap",
        "updated_at",
        "updated_by",
    )
    readonly_fields = ("updated_at",)
    autocomplete_fields = ("updated_by",)
    fieldsets = (
        (
            "Development & messaging",
            {"fields": ("development_mode", "msg91_authkey")},
        ),
        (
            "Pricing & MLM",
            {
                "fields": (
                    "product_base_price",
                    "gst_rate",
                    "direct_commission",
                    "upline_commission",
                    "earning_cap",
                    "sponsor_slot_expiry_days",
                    "is_repurchase_commission_allowed",
                    "auto_process_milestone_bonuses",
                    "milestone_bonus_overrides",
                    "default_company_referral_code",
                )
            },
        ),
        (
            "TDS & withdrawals",
            {
                "fields": (
                    "tds_194h_rate",
                    "tds_194r_rate",
                    "tds_cash_trigger",
                    "cooling_off_days",
                    "refund_window_days",
                )
            },
        ),
        (
            "Placement",
            {
                "fields": (
                    "placement_manual_window_hours",
                    "auto_placement_strategy",
                    "trigger_instant_kyc_submission",
                )
            },
        ),
        (
            "Razorpay",
            {"fields": ("razorpay_key_id", "razorpay_key_secret")},
        ),
        (
            "Nodal / SLA",
            {
                "fields": (
                    "nodal_officer_name",
                    "nodal_officer_email",
                    "nodal_officer_phone",
                    "grievance_sla_hours",
                    "refund_request_sla_hours",
                )
            },
        ),
        (
            "App versions",
            {
                "fields": (
                    "ios_latest_app_version",
                    "ios_force_update",
                    "android_latest_app_version",
                    "android_force_update",
                    "play_store_url",
                    "app_store_url",
                )
            },
        ),
        ("Meta", {"fields": ("updated_by", "updated_at")}),
    )

    def has_add_permission(self, request):
        if SystemConfig.objects.exists():
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Grievance)
class GrievanceAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "subject", "status", "created_at", "updated_at")
    list_filter = ("status", "created_at")
    search_fields = (
        "subject",
        "body",
        "user__member_id",
        "user__full_name",
        "user__phone",
        "user__email",
    )
    autocomplete_fields = ("user",)
    readonly_fields = ("created_at", "updated_at")
    ordering = ("-id",)
