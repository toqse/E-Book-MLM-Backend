from django.contrib import admin

from .models import Cart, CartItem


class CartItemInline(admin.TabularInline):
    model = CartItem
    extra = 0
    autocomplete_fields = ("ebook",)
    readonly_fields = ("created_at",)


@admin.register(Cart)
class CartAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "updated_at")
    search_fields = (
        "user__member_id",
        "user__full_name",
        "user__phone",
        "user__email",
    )
    autocomplete_fields = ("user",)
    readonly_fields = ("updated_at",)
    inlines = (CartItemInline,)
    ordering = ("-updated_at",)


@admin.register(CartItem)
class CartItemAdmin(admin.ModelAdmin):
    list_display = ("id", "cart", "ebook", "created_at")
    search_fields = (
        "cart__user__member_id",
        "ebook__title",
        "ebook__slug",
    )
    autocomplete_fields = ("cart", "ebook")
    readonly_fields = ("created_at",)
    ordering = ("-id",)
