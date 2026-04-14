from django.contrib.auth import get_user_model

from .models import AuditLog

User = get_user_model()


def write_audit(
    action: str,
    *,
    actor=None,
    target_type: str = "",
    target_id: str = "",
    payload=None,
    ip_address=None,
):
    AuditLog.objects.create(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        payload=payload or {},
        ip_address=ip_address,
    )
