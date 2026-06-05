from django.db import transaction

from .models import SystemConfig


def get_system_config() -> SystemConfig:
    with transaction.atomic():
        obj, _ = SystemConfig.objects.select_for_update().get_or_create(pk=1)
    return obj


def is_development_mode() -> bool:
    return bool(get_system_config().development_mode)


def get_msg91_authkey() -> str:
    return (get_system_config().msg91_authkey or "").strip()
