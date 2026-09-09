"""MSG91 notification when a member reaches a milestone threshold."""

from __future__ import annotations

from decimal import Decimal

from apps.commissions.milestone_tiers import milestone_display_name
from apps.users.models import User


def schedule_milestone_achieved_notification(
    *,
    user: User,
    threshold: int,
    bonus_amount,
    cfg=None,
) -> None:
    from apps.commissions.milestone_tiers import milestone_tier_index
    from apps.notifications.lifecycle import schedule_msg91_lifecycle
    from apps.notifications.msg91 import _amount_str
    from apps.notifications.tasks import send_milestone_achieved_task

    tier_idx = milestone_tier_index(threshold, cfg)
    label = milestone_display_name(threshold, tier_index=tier_idx, cfg=cfg)
    amount = _amount_str(bonus_amount if bonus_amount is not None else Decimal("0"))
    schedule_msg91_lifecycle(
        send_milestone_achieved_task,
        user.pk,
        int(threshold),
        amount,
        label,
    )
