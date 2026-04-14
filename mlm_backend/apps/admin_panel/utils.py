from django.db import transaction

from .models import SystemConfig


def get_system_config() -> SystemConfig:
    with transaction.atomic():
        obj, _ = SystemConfig.objects.select_for_update().get_or_create(pk=1)
    return obj
