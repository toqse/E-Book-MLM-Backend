from django.contrib.auth.base_user import BaseUserManager


class UserManager(BaseUserManager):
    use_in_migrations = True

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
        return self._create_user(login_identifier, password, **extra_fields)
