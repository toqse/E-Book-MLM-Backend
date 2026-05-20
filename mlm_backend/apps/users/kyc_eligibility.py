"""KYC submission timing (refund window vs instant) and MLM feature unlock rules."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from apps.admin_panel.utils import get_system_config
from apps.agreements.models import MemberComplianceProfile
from apps.payments.models import Order, OrderLine
from apps.users.models import User


def _paid_ebook_orders_qs(user: User):
    return Order.objects.filter(user=user, status=Order.Status.PAID).filter(
        Q(ebook_id__isnull=False)
        | Exists(OrderLine.objects.filter(order_id=OuterRef("pk")))
    )


def user_has_qualifying_paid_ebook_purchase(user: User) -> bool:
    return _paid_ebook_orders_qs(user).exists()


def _order_refund_cutoff(order: Order):
    if order.refund_eligible_until:
        return order.refund_eligible_until
    if order.paid_at:
        cfg = get_system_config()
        return order.paid_at + timedelta(days=int(cfg.refund_window_days or 0))
    return None


def user_refund_window_closed_for_any_purchase(user: User) -> bool:
    now = timezone.now()
    for o in _paid_ebook_orders_qs(user).only("refund_eligible_until", "paid_at"):
        cutoff = _order_refund_cutoff(o)
        if cutoff and now >= cutoff:
            return True
    return False


def is_instant_kyc_submission_enabled() -> bool:
    return bool(get_system_config().trigger_instant_kyc_submission)


def user_kyc_submission_allowed(user: User) -> bool:
    if not user_has_qualifying_paid_ebook_purchase(user):
        return False
    if is_instant_kyc_submission_enabled():
        return True
    return user_refund_window_closed_for_any_purchase(user)


def user_mlm_features_unlocked(user: User) -> bool:
    if user.kyc_status != User.KYCStatus.VERIFIED:
        return False
    return MemberComplianceProfile.objects.filter(user=user).exists()


def _earliest_open_refund_eligible_at(user: User):
    now = timezone.now()
    earliest = None
    for o in _paid_ebook_orders_qs(user).only("refund_eligible_until", "paid_at"):
        cutoff = _order_refund_cutoff(o)
        if cutoff and cutoff > now:
            if earliest is None or cutoff < earliest:
                earliest = cutoff
    return earliest


def user_kyc_submission_mode() -> str:
    return "instant" if is_instant_kyc_submission_enabled() else "after_refund"


def user_kyc_invitation_should_send(user: User) -> bool:
    if is_instant_kyc_submission_enabled():
        return False
    if user.kyc_status == User.KYCStatus.VERIFIED:
        return False
    if user.kyc_invitation_sent_at:
        return False
    if not user_has_qualifying_paid_ebook_purchase(user):
        return False
    return user_refund_window_closed_for_any_purchase(user)


def kyc_submission_blocked_response():
    from apps.common.responses import envelope_response

    if is_instant_kyc_submission_enabled():
        message = "Complete a book purchase before submitting KYC."
    else:
        message = "KYC opens after the refund period for your purchase."
    return envelope_response(
        None,
        message=message,
        success=False,
        errors={"detail": "kyc_refund_window_active"},
        status=403,
    )


def _kyc_notice_message_and_code(user: User, ctx: dict[str, Any]) -> tuple[str | None, str | None]:
    if ctx["mlm_features_unlocked"]:
        return None, None
    if user.kyc_status == User.KYCStatus.VERIFIED:
        return None, None

    if user.kyc_status == User.KYCStatus.REJECTED:
        reason = (user.kyc_rejection_reason or "").strip()
        msg = "KYC was rejected. Update your documents and resubmit."
        if reason:
            msg = f"{msg} Reason: {reason}"
        return msg, "rejected"

    profile_exists = MemberComplianceProfile.objects.filter(user=user).exists()
    if user.kyc_status == User.KYCStatus.PENDING and profile_exists:
        return "KYC is under admin review. Team and earnings unlock after approval.", "pending_review"

    if not ctx["kyc_submission_allowed"]:
        if not user_has_qualifying_paid_ebook_purchase(user):
            return "Purchase a book to unlock KYC submission.", "purchase_required"
        if ctx["kyc_submission_mode"] == "after_refund":
            eligible_at = ctx.get("kyc_eligible_at")
            if eligible_at:
                label = eligible_at.strftime("%d/%m/%Y")
                return (
                    f"KYC opens after your refund period ends on {label}.",
                    "refund_window",
                )
            return "KYC opens after the refund period for your purchase.", "refund_window"
        return "Complete a book purchase before submitting KYC.", "purchase_required"

    return (
        "Complete KYC & compliance to unlock team network, earnings, withdrawals, milestones, and sponsor slots.",
        "submit_kyc",
    )


def user_kyc_access_context(user: User) -> dict[str, Any]:
    allowed = user_kyc_submission_allowed(user)
    mode = user_kyc_submission_mode()
    mlm_unlocked = user_mlm_features_unlocked(user)
    eligible_at = None
    if not allowed and mode == "after_refund":
        eligible_at = _earliest_open_refund_eligible_at(user)
    ctx: dict[str, Any] = {
        "kyc_submission_allowed": allowed,
        "kyc_submission_mode": mode,
        "trigger_instant_kyc_submission": mode == "instant",
        "kyc_eligible_at": eligible_at,
        "mlm_features_unlocked": mlm_unlocked,
    }
    msg, code = _kyc_notice_message_and_code(user, ctx)
    ctx["kyc_notice_message"] = msg
    ctx["kyc_notice_code"] = code
    return ctx
