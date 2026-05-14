import secrets
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

    def _qs(self):
        return self.model._default_manager.using(self._db)

    def _next_unique_member_id(self, preferred: str) -> str:
        preferred = (preferred or "SYS")[:32]
        if not self._qs().filter(member_id=preferred).exists():
            return preferred
        for _ in range(128):
            candidate = f"SYS{secrets.token_hex(6)}"[:32]
            if not self._qs().filter(member_id=candidate).exists():
                return candidate
        raise RuntimeError("Unable to allocate a unique member_id for superuser.")

    def _next_unique_referral_code(self, preferred: str) -> str:
        preferred = (preferred or "X")[:16]
        if not self._qs().filter(referral_code=preferred).exists():
            return preferred
        for _ in range(128):
            candidate = secrets.token_hex(8)[:16]
            if not self._qs().filter(referral_code=candidate).exists():
                return candidate
        raise RuntimeError("Unable to allocate a unique referral_code for superuser.")

    def create_superuser(self, login_identifier, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("role", "SUPER_ADMIN")
        extra_fields.setdefault("full_name", "Company Administrator")
        ref_default = getattr(settings, "DEFAULT_COMPANY_REFERRAL_CODE", "Admin") or "Admin"
        mid_default = getattr(settings, "COMPANY_SUPERUSER_MEMBER_ID", "SYS000001")
        if not extra_fields.get("member_id"):
            extra_fields["member_id"] = self._next_unique_member_id(mid_default)
        if not extra_fields.get("referral_code"):
            extra_fields["referral_code"] = self._next_unique_referral_code(ref_default)
        if not extra_fields.get("referral_link"):
            base = getattr(settings, "FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/") + "/"
            rc = extra_fields["referral_code"]
            extra_fields["referral_link"] = urljoin(base, f"join?ref={rc}")
        return self._create_user(login_identifier, password, **extra_fields)
