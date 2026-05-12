from __future__ import annotations

from typing import Any

from .models import SponsorSlotAuditEvent, SponsorSlotCode


def log_sponsor_audit(
    code: SponsorSlotCode,
    event_type: str,
    *,
    actor=None,
    metadata: dict[str, Any] | None = None,
) -> SponsorSlotAuditEvent:
    return SponsorSlotAuditEvent.objects.create(
        sponsor_slot_code=code,
        event_type=event_type,
        actor=actor,
        metadata=metadata or {},
    )
