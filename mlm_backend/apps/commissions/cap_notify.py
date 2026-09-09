"""MSG91 notification when a member first reaches the earning cap."""

from __future__ import annotations

from decimal import Decimal

from apps.users.models import User


def maybe_schedule_earning_cap_notification(*, user: User, wallet, cap) -> None:
    """Notify once when account_status transitions to CAPPED."""
    if user.account_status == User.AccountStatus.CAPPED:
        return
    cap_dec = Decimal(str(cap or 0))
    total = Decimal(str(wallet.total_earned or 0))
    if cap_dec <= 0 or total < cap_dec:
        return

    from apps.notifications.lifecycle import schedule_msg91_lifecycle
    from apps.notifications.tasks import send_earning_cap_achieved_task

    user.account_status = User.AccountStatus.CAPPED
    user.save(update_fields=["account_status", "updated_at"])
    schedule_msg91_lifecycle(send_earning_cap_achieved_task, user.pk, str(total))
