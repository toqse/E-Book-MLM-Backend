"""Post-refund KYC invitation delivery (MSG91 campaign or dev-mode SMTP/log stub)."""

from __future__ import annotations

import logging

from django.conf import settings

from apps.admin_panel.utils import get_msg91_authkey, is_development_mode
from apps.agreements.kyc_invite_token import build_kyc_invite_token
from apps.notifications.models import NotificationLog
from apps.users.models import User

_logger = logging.getLogger(__name__)


def build_kyc_invite_url(*, user_id: int) -> str:
    base = getattr(settings, "FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/")
    path = getattr(settings, "KYC_INVITE_WEB_PATH", "/compliance")
    if not path.startswith("/"):
        path = f"/{path}"
    token = build_kyc_invite_token(user_id=user_id)
    return f"{base}{path}?kyc_token={token}"


def _send_email_invitation(*, user: User, link: str) -> bool:
    subject = "Complete your KYC & compliance verification"
    body = (
        f"Hello {user.full_name or 'Member'},\n\n"
        "Your refund period has ended. You can now complete KYC and compliance "
        "to unlock team network, earnings, withdrawals, milestones, and sponsor slots.\n\n"
        f"Open this link to continue:\n{link}\n\n"
        "If you did not make this purchase, contact support."
    )
    recipient = (user.email or "").strip()
    if not recipient:
        _logger.info(
            "KYC invitation (no email on file) user_id=%s link=%s",
            user.pk,
            link,
        )
        return False

    host = getattr(settings, "EMAIL_HOST", "") or ""
    if host:
        from django.core.mail import send_mail

        send_mail(
            subject,
            body,
            getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@localhost"),
            [recipient],
            fail_silently=False,
        )
        return True

    _logger.info(
        "KYC invitation email (SMTP not configured) to=%s user_id=%s link=%s",
        recipient,
        user.pk,
        link,
    )
    return True


def _send_sms_invitation(*, user: User, link: str) -> bool:
    phone = (user.phone or "").strip()
    if not phone:
        return False
    _logger.info(
        "KYC invitation SMS stub user_id=%s phone=%s link=%s",
        user.pk,
        phone,
        link,
    )
    return False


def deliver_kyc_invitation(*, user: User) -> dict:
    """Send invitation channels; returns payload for NotificationLog."""
    link = build_kyc_invite_url(user_id=user.pk)
    mobile_link = getattr(settings, "KYC_INVITE_MOBILE_URL", "").strip() or link

    if not is_development_mode() and get_msg91_authkey():
        from apps.notifications import msg91

        sent = msg91.send_invitation_message(
            name=user.full_name or "Member",
            email=(user.email or "").strip() or None,
            mobile=user.phone,
        )
        NotificationLog.objects.create(
            user=user,
            channel="MSG91" if sent else "LOG",
            template_key="kyc_invitation",
            payload={"link": link, "msg91_sent": sent},
        )
        return {"link": link, "email_sent": sent, "sms_sent": sent, "msg91_sent": sent}

    email_sent = _send_email_invitation(user=user, link=link)
    sms_sent = _send_sms_invitation(user=user, link=mobile_link)
    NotificationLog.objects.create(
        user=user,
        channel="EMAIL" if email_sent else ("SMS" if sms_sent else "LOG"),
        template_key="kyc_invitation",
        payload={"link": link, "email_sent": email_sent, "sms_sent": sms_sent},
    )
    return {"link": link, "email_sent": email_sent, "sms_sent": sms_sent}
