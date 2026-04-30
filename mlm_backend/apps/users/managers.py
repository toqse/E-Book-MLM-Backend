from urllib.parse import urljoin

from django.conf import settings
from django.contrib.auth.base_user import BaseUserManager


class UserManager(BaseUserManager):
    use_in_migrations = True

    def get_by_natural_key(self, username):
        """Admin / ModelBackend login: match login_identifier (emails case-insensitive)."""
        if username is None:
            raise self.model.DoesNotExist
        key = str(username).strip()
        if "@" in key:
            key = key.lower()
        return self.get(login_identifier__iexact=key)

    def _create_user(self, login_identifier, password, **extra_fields):
        if not login_identifier:
            raise ValueError("login_identifier is required")
        user = self.model(login_identifier=login_identifier, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, login_identifier, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(login_identifier, password, **extra_fields)

    def create_superuser(self, login_identifier, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("role", "SUPER_ADMIN")
        extra_fields.setdefault("full_name", "Company Administrator")
        ref_default = getattr(settings, "DEFAULT_COMPANY_REFERRAL_CODE", "Admin")
        mid_default = getattr(settings, "COMPANY_SUPERUSER_MEMBER_ID", "SYS000001")
        if not extra_fields.get("member_id"):
            extra_fields["member_id"] = mid_default
        if not extra_fields.get("referral_code"):
            extra_fields["referral_code"] = ref_default
        if not extra_fields.get("referral_link"):
            base = getattr(settings, "FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/") + "/"
            rc = extra_fields.get("referral_code", ref_default)
            extra_fields["referral_link"] = urljoin(base, f"join?ref={rc}")
        return self._create_user(login_identifier, password, **extra_fields)
