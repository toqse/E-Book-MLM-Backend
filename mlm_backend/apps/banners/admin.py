from django.contrib import admin

from .models import Banner


@admin.register(Banner)
class BannerAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "is_active", "sort_order", "created_at")
    list_filter = ("is_active",)
    search_fields = ("title", "link_url")
    ordering = ("sort_order", "-id")

