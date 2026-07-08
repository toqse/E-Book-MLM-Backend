import logging

from config.celery import app

_logger = logging.getLogger(__name__)


def _msg91_lifecycle_allowed() -> bool:
    from apps.admin_panel.utils import get_msg91_authkey, is_development_mode

    if is_development_mode():
        return False
    if not get_msg91_authkey():
        return False
    return True


def _user_contact_payload(user) -> dict:
    return {
        "name": user.full_name or "Member",
        "email": (user.email or "").strip() or None,
        "mobile": user.phone,
    }


def _log_lifecycle_notification(*, user, template_key: str, payload: dict, sent: bool) -> None:
    from apps.notifications.models import NotificationLog

    NotificationLog.objects.create(
        user=user,
        channel="MSG91" if sent else "LOG",
        template_key=template_key,
        payload={**payload, "msg91_sent": sent},
    )


@app.task
def send_otp_sms_task(phone: str, code: str):
    # Legacy stub — OTP delivery is handled via apps.notifications.msg91.
    return phone, code


@app.task
def notify_commission_credited(user_id: int, amount: str):
    return user_id, amount


@app.task
def send_kyc_invitation_sms_task(phone: str, link: str):
    # Legacy stub — KYC invitation delivery is handled via apps.notifications.msg91.
    return phone, link


@app.task
def send_invoice_message_task(order_id: int) -> bool:
    """Send GST invoice via MSG91 after payment (async; does not block checkout)."""
    from apps.admin_panel.utils import is_development_mode
    from apps.notifications import msg91
    from apps.payments.invoice_links import build_public_invoice_download_url
    from apps.payments.models import GSTInvoice, Order
    from apps.payments.services import ensure_gst_invoice_pdf

    if is_development_mode():
        _logger.info("Invoice MSG91 skipped order_id=%s (development_mode)", order_id)
        return False

    order = (
        Order.objects.select_related("user")
        .filter(pk=order_id, status=Order.Status.PAID)
        .first()
    )
    if not order:
        _logger.warning("Invoice MSG91 skipped: order not found or not PAID order_id=%s", order_id)
        return False

    inv = GSTInvoice.objects.filter(order_id=order.pk).first()
    if not inv:
        _logger.warning("Invoice MSG91 skipped: no GSTInvoice order_id=%s", order_id)
        return False

    try:
        ensure_gst_invoice_pdf(order)
    except Exception:
        _logger.exception("Invoice MSG91 PDF ensure failed order_id=%s", order_id)

    user = order.user
    invoice_link = build_public_invoice_download_url(user_id=user.pk, order_id=order.pk)
    if not invoice_link:
        _logger.warning(
            "Invoice MSG91 skipped: PUBLIC_BACKEND_BASE_URL not set order_id=%s",
            order_id,
        )
        return False

    paid_at = order.paid_at
    if paid_at:
        invoice_date = paid_at.strftime("%d-%b-%Y")
    else:
        from django.utils import timezone

        invoice_date = timezone.now().strftime("%d-%b-%Y")

    sent = msg91.send_invoice_message(
        name=user.full_name or "Member",
        email=(user.email or "").strip() or None,
        mobile=user.phone,
        invoice_number=inv.invoice_number,
        invoice_date=invoice_date,
        amount=str(order.amount_paid),
        invoice_link=invoice_link,
    )
    if sent:
        from apps.notifications.models import NotificationLog

        NotificationLog.objects.create(
            user=user,
            channel="MSG91",
            template_key="invoice_purchase",
            payload={
                "order_id": order.pk,
                "invoice_number": inv.invoice_number,
                "invoice_link": invoice_link,
            },
        )
    return sent


@app.task
def send_welcome_registration_task(user_id: int) -> bool:
    from apps.notifications import msg91
    from apps.users.models import User

    if not _msg91_lifecycle_allowed():
        _logger.info("Welcome MSG91 skipped user_id=%s (development_mode or no authkey)", user_id)
        return False

    user = User.objects.filter(pk=user_id).first()
    if not user:
        _logger.warning("Welcome MSG91 skipped: user not found user_id=%s", user_id)
        return False

    contact = _user_contact_payload(user)
    sent = msg91.send_welcome_registration_message(**contact)
    _log_lifecycle_notification(
        user=user,
        template_key="welcome_registration",
        payload={"user_id": user_id},
        sent=sent,
    )
    return sent


@app.task
def send_kyc_submitted_task(user_id: int) -> bool:
    from apps.notifications import msg91
    from apps.users.models import User

    if not _msg91_lifecycle_allowed():
        _logger.info("KYC submitted MSG91 skipped user_id=%s (development_mode or no authkey)", user_id)
        return False

    user = User.objects.filter(pk=user_id).first()
    if not user:
        _logger.warning("KYC submitted MSG91 skipped: user not found user_id=%s", user_id)
        return False

    submitted_at = user.kyc_submitted_at
    if submitted_at:
        submitted_date = submitted_at.strftime("%d-%m-%Y")
    else:
        from django.utils import timezone

        submitted_date = timezone.now().strftime("%d-%m-%Y")

    contact = _user_contact_payload(user)
    sent = msg91.send_kyc_submitted_message(**contact, submitted_date=submitted_date)
    _log_lifecycle_notification(
        user=user,
        template_key="kyc_submitted",
        payload={"user_id": user_id, "submitted_date": submitted_date},
        sent=sent,
    )
    return sent


@app.task
def send_kyc_approved_task(user_id: int) -> bool:
    from apps.notifications import msg91
    from apps.users.models import User

    if not _msg91_lifecycle_allowed():
        _logger.info("KYC approved MSG91 skipped user_id=%s (development_mode or no authkey)", user_id)
        return False

    user = User.objects.filter(pk=user_id).first()
    if not user:
        _logger.warning("KYC approved MSG91 skipped: user not found user_id=%s", user_id)
        return False

    contact = _user_contact_payload(user)
    sent = msg91.send_kyc_approved_message(**contact)
    _log_lifecycle_notification(
        user=user,
        template_key="kyc_approved",
        payload={"user_id": user_id},
        sent=sent,
    )
    return sent


@app.task
def send_kyc_rejected_task(user_id: int, rejection_reason: str) -> bool:
    from apps.notifications import msg91
    from apps.users.models import User

    if not _msg91_lifecycle_allowed():
        _logger.info("KYC rejected MSG91 skipped user_id=%s (development_mode or no authkey)", user_id)
        return False

    user = User.objects.filter(pk=user_id).first()
    if not user:
        _logger.warning("KYC rejected MSG91 skipped: user not found user_id=%s", user_id)
        return False

    reason = (rejection_reason or user.kyc_rejection_reason or "").strip()
    contact = _user_contact_payload(user)
    sent = msg91.send_kyc_rejected_message(**contact, rejection_reason=reason)
    _log_lifecycle_notification(
        user=user,
        template_key="kyc_rejected",
        payload={"user_id": user_id, "rejection_reason": reason},
        sent=sent,
    )
    return sent


@app.task
def send_referral_joined_task(sponsor_id: int, referral_user_id: int) -> bool:
    from apps.notifications import msg91
    from apps.users.models import User

    if not _msg91_lifecycle_allowed():
        _logger.info(
            "Referral joined MSG91 skipped sponsor_id=%s (development_mode or no authkey)",
            sponsor_id,
        )
        return False

    sponsor = User.objects.filter(pk=sponsor_id).first()
    referral = User.objects.filter(pk=referral_user_id).first()
    if not sponsor or not referral:
        _logger.warning(
            "Referral joined MSG91 skipped: sponsor_id=%s referral_user_id=%s not found",
            sponsor_id,
            referral_user_id,
        )
        return False

    contact = _user_contact_payload(sponsor)
    sent = msg91.send_referral_joined_message(
        **contact,
        referral_name=referral.full_name or "Member",
    )
    _log_lifecycle_notification(
        user=sponsor,
        template_key="referral_joined",
        payload={"sponsor_id": sponsor_id, "referral_user_id": referral_user_id},
        sent=sent,
    )
    return sent


@app.task
def send_milestone_achieved_task(
    user_id: int,
    threshold: int,
    reward_amount: str,
    milestone_label: str,
) -> bool:
    from apps.notifications import msg91
    from apps.users.models import User

    if not _msg91_lifecycle_allowed():
        _logger.info(
            "Milestone achieved MSG91 skipped user_id=%s (development_mode or no authkey)",
            user_id,
        )
        return False

    user = User.objects.filter(pk=user_id).first()
    if not user:
        _logger.warning("Milestone achieved MSG91 skipped: user not found user_id=%s", user_id)
        return False

    contact = _user_contact_payload(user)
    sent = msg91.send_milestone_achieved_message(
        **contact,
        reward_amount=reward_amount,
        milestone_label=milestone_label,
    )
    _log_lifecycle_notification(
        user=user,
        template_key="milestone_achieved",
        payload={
            "user_id": user_id,
            "threshold": threshold,
            "reward_amount": reward_amount,
            "milestone_label": milestone_label,
        },
        sent=sent,
    )
    return sent


@app.task
def send_earning_cap_achieved_task(user_id: int, total_earnings: str) -> bool:
    from apps.notifications import msg91
    from apps.users.models import User

    if not _msg91_lifecycle_allowed():
        _logger.info(
            "Earning cap MSG91 skipped user_id=%s (development_mode or no authkey)",
            user_id,
        )
        return False

    user = User.objects.filter(pk=user_id).first()
    if not user:
        _logger.warning("Earning cap MSG91 skipped: user not found user_id=%s", user_id)
        return False

    contact = _user_contact_payload(user)
    sent = msg91.send_earning_cap_achieved_message(**contact, total_earnings=total_earnings)
    _log_lifecycle_notification(
        user=user,
        template_key="earning_cap_achieved",
        payload={"user_id": user_id, "total_earnings": total_earnings},
        sent=sent,
    )
    return sent


@app.task
def send_withdrawal_submitted_task(withdrawal_id: int) -> bool:
    from apps.notifications import msg91
    from apps.wallet.models import WithdrawalRequest

    if not _msg91_lifecycle_allowed():
        _logger.info(
            "Withdrawal submitted MSG91 skipped withdrawal_id=%s (development_mode or no authkey)",
            withdrawal_id,
        )
        return False

    wr = WithdrawalRequest.objects.select_related("user").filter(pk=withdrawal_id).first()
    if not wr:
        _logger.warning(
            "Withdrawal submitted MSG91 skipped: not found withdrawal_id=%s",
            withdrawal_id,
        )
        return False

    user = wr.user
    amount = msg91._amount_str(wr.amount_requested)
    request_date = wr.created_at.strftime("%d-%m-%Y")
    contact = _user_contact_payload(user)
    sent = msg91.send_withdrawal_submitted_message(
        **contact,
        amount=amount,
        request_date=request_date,
    )
    _log_lifecycle_notification(
        user=user,
        template_key="withdrawal_submitted",
        payload={"withdrawal_id": withdrawal_id, "amount": amount, "request_date": request_date},
        sent=sent,
    )
    return sent


@app.task
def send_withdrawal_approved_task(withdrawal_id: int) -> bool:
    from apps.notifications import msg91
    from apps.wallet.models import WithdrawalRequest

    if not _msg91_lifecycle_allowed():
        _logger.info(
            "Withdrawal approved MSG91 skipped withdrawal_id=%s (development_mode or no authkey)",
            withdrawal_id,
        )
        return False

    wr = WithdrawalRequest.objects.select_related("user").filter(pk=withdrawal_id).first()
    if not wr:
        _logger.warning(
            "Withdrawal approved MSG91 skipped: not found withdrawal_id=%s",
            withdrawal_id,
        )
        return False

    user = wr.user
    amount = msg91._amount_str(wr.amount_requested)
    contact = _user_contact_payload(user)
    sent = msg91.send_withdrawal_approved_message(**contact, amount=amount)
    _log_lifecycle_notification(
        user=user,
        template_key="withdrawal_approved",
        payload={"withdrawal_id": withdrawal_id, "amount": amount},
        sent=sent,
    )
    return sent


@app.task
def send_withdrawal_rejected_task(withdrawal_id: int, rejection_reason: str) -> bool:
    from apps.notifications import msg91
    from apps.wallet.models import WithdrawalRequest

    if not _msg91_lifecycle_allowed():
        _logger.info(
            "Withdrawal rejected MSG91 skipped withdrawal_id=%s (development_mode or no authkey)",
            withdrawal_id,
        )
        return False

    wr = WithdrawalRequest.objects.select_related("user").filter(pk=withdrawal_id).first()
    if not wr:
        _logger.warning(
            "Withdrawal rejected MSG91 skipped: not found withdrawal_id=%s",
            withdrawal_id,
        )
        return False

    user = wr.user
    reason = (rejection_reason or wr.reject_reason or "").strip()
    contact = _user_contact_payload(user)
    sent = msg91.send_withdrawal_rejected_message(**contact, rejection_reason=reason)
    _log_lifecycle_notification(
        user=user,
        template_key="withdrawal_rejected",
        payload={"withdrawal_id": withdrawal_id, "rejection_reason": reason},
        sent=sent,
    )
    return sent


@app.task
def send_refund_approved_task(refund_request_id: int) -> bool:
    from apps.notifications import msg91
    from apps.payments.models import RefundRequest

    if not _msg91_lifecycle_allowed():
        _logger.info(
            "Refund approved MSG91 skipped refund_request_id=%s (development_mode or no authkey)",
            refund_request_id,
        )
        return False

    rr = RefundRequest.objects.select_related("user").filter(pk=refund_request_id).first()
    if not rr:
        _logger.warning(
            "Refund approved MSG91 skipped: not found refund_request_id=%s",
            refund_request_id,
        )
        return False

    user = rr.user
    amount = msg91._amount_str(rr.amount)
    contact = _user_contact_payload(user)
    sent = msg91.send_refund_approved_message(**contact, refund_amount=amount)
    _log_lifecycle_notification(
        user=user,
        template_key="refund_approved",
        payload={"refund_request_id": refund_request_id, "refund_amount": amount},
        sent=sent,
    )
    return sent


@app.task
def send_refund_rejected_task(refund_request_id: int, rejection_reason: str) -> bool:
    from apps.notifications import msg91
    from apps.payments.models import RefundRequest

    if not _msg91_lifecycle_allowed():
        _logger.info(
            "Refund rejected MSG91 skipped refund_request_id=%s (development_mode or no authkey)",
            refund_request_id,
        )
        return False

    rr = RefundRequest.objects.select_related("user").filter(pk=refund_request_id).first()
    if not rr:
        _logger.warning(
            "Refund rejected MSG91 skipped: not found refund_request_id=%s",
            refund_request_id,
        )
        return False

    user = rr.user
    reason = (rejection_reason or rr.reject_reason or "").strip()
    contact = _user_contact_payload(user)
    sent = msg91.send_refund_rejected_message(**contact, rejection_reason=reason)
    _log_lifecycle_notification(
        user=user,
        template_key="refund_rejected",
        payload={"refund_request_id": refund_request_id, "rejection_reason": reason},
        sent=sent,
    )
    return sent
