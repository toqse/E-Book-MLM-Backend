from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("member_id", "login_identifier", "full_name", "role", "is_member", "account_status")
    search_fields = ("member_id", "phone", "email", "referral_code")
    ordering = ("member_id",)
