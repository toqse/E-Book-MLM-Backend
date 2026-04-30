from django.contrib import admin

from .models import EBook, Enrollment


@admin.register(EBook)
class EBookAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "slug", "category", "price", "status", "is_active")
    list_filter = ("status", "is_active", "is_primary", "category")
    search_fields = ("title", "slug", "category")
    readonly_fields = ("created_at",)
    ordering = ("-id",)


@admin.register(Enrollment)
class EnrollmentAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "ebook", "order", "is_retail", "download_count", "created_at")
    list_filter = ("is_retail", "created_at")
    search_fields = ("user__member_id", "user__phone", "ebook__title", "order__order_number")
    autocomplete_fields = ("user", "ebook", "order")
    readonly_fields = ("created_at",)
    ordering = ("-id",)
