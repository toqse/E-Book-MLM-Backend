"""Send or resend post-refund KYC invitation emails/SMS (admin + Celery)."""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.notifications.kyc_invitation import deliver_kyc_invitation
from apps.users.kyc_eligibility import (
    is_instant_kyc_submission_enabled,
    user_has_qualifying_paid_ebook_purchase,
    user_refund_window_closed_for_any_purchase,
)
from apps.users.models import User


def kyc_invitation_block_reason(
    user: User,
    *,
    force: bool = False,
    skip_refund_check: bool = False,
) -> str | None:
    """Return a machine-readable reason when send is not allowed, else None."""
    if is_instant_kyc_submission_enabled():
        return "instant_kyc_mode"
    if user.kyc_status == User.KYCStatus.VERIFIED:
        return "kyc_already_verified"
    if not user_has_qualifying_paid_ebook_purchase(user):
        return "no_paid_ebook_purchase"
    if not skip_refund_check and not user_refund_window_closed_for_any_purchase(user):
        return "refund_window_active"
    if user.kyc_invitation_sent_at and not force:
        return "already_sent"
    return None


def send_kyc_invitation_to_user(
    user_id: int,
    *,
    force: bool = False,
    skip_refund_check: bool = False,
) -> dict[str, Any]:
    """
    Deliver KYC invitation and set kyc_invitation_sent_at.

    Returns dict with sent (bool), user_id, reason (if blocked), link, email_sent, sms_sent,
    kyc_invitation_sent_at, resent (bool).
    """
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        return {
            "sent": False,
            "user_id": user_id,
            "reason": "not_found",
        }

    reason = kyc_invitation_block_reason(
        user, force=force, skip_refund_check=skip_refund_check
    )
    if reason:
        return {
            "sent": False,
            "user_id": user_id,
            "member_id": user.member_id,
            "reason": reason,
            "kyc_invitation_sent_at": (
                user.kyc_invitation_sent_at.isoformat()
                if user.kyc_invitation_sent_at
                else None
            ),
        }

    was_sent_before = bool(user.kyc_invitation_sent_at)

    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user_id)
        reason = kyc_invitation_block_reason(
            user, force=force, skip_refund_check=skip_refund_check
        )
        if reason:
            return {
                "sent": False,
                "user_id": user_id,
                "member_id": user.member_id,
                "reason": reason,
                "kyc_invitation_sent_at": (
                    user.kyc_invitation_sent_at.isoformat()
                    if user.kyc_invitation_sent_at
                    else None
                ),
            }
        delivery = deliver_kyc_invitation(user=user)
        now = timezone.now()
        user.kyc_invitation_sent_at = now
        user.save(update_fields=["kyc_invitation_sent_at", "updated_at"])

    return {
        "sent": True,
        "user_id": user_id,
        "member_id": user.member_id,
        "resent": was_sent_before,
        "link": delivery.get("link"),
        "email_sent": delivery.get("email_sent"),
        "sms_sent": delivery.get("sms_sent"),
        "kyc_invitation_sent_at": now.isoformat(),
    }
