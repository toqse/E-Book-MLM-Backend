import logging

from config.celery import app

_logger = logging.getLogger(__name__)


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
