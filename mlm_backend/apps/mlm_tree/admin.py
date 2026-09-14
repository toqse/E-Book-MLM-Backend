from django.contrib import admin

from .models import BinaryNode


@admin.register(BinaryNode)
class BinaryNodeAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "parent",
        "position",
        "level",
        "left_subtree_size",
        "right_subtree_size",
        "created_at",
    )
    list_filter = ("position", "level")
    search_fields = (
        "user__member_id",
        "user__full_name",
        "user__phone",
        "user__email",
    )
    autocomplete_fields = (
        "user",
        "parent",
        "left_child",
        "right_child",
        "upline_l1",
        "upline_l2",
        "upline_l3",
    )
    readonly_fields = ("created_at",)
    ordering = ("id",)
