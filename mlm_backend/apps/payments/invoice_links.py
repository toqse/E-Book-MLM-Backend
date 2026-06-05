"""Signed invoice download links (shared by API views and MSG91 notifications)."""

from __future__ import annotations

from django.conf import settings
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner

_signer = TimestampSigner(salt="payments.invoice.download")


def invoice_link_max_age_seconds() -> int:
    days = int(getattr(settings, "MSG91_INVOICE_LINK_TTL_DAYS", 30))
    return max(1, days) * 86400


def sign_invoice_download_token(*, user_id: int, order_id: int) -> str:
    return _signer.sign(f"{user_id}:{order_id}")


def build_public_invoice_download_url(*, user_id: int, order_id: int) -> str | None:
    base = (getattr(settings, "PUBLIC_BACKEND_BASE_URL", "") or "").strip().rstrip("/")
    if not base:
        return None
    token = sign_invoice_download_token(user_id=user_id, order_id=order_id)
    return f"{base}/api/v1/user/orders/{order_id}/invoice/?token={token}"


def verify_invoice_download_token(token: str, *, order_id: int) -> int | None:
    try:
        payload = _signer.unsign(token, max_age=invoice_link_max_age_seconds())
        user_id_s, order_id_s = payload.split(":", 1)
        if int(order_id_s) != int(order_id):
            return None
        return int(user_id_s)
    except (BadSignature, SignatureExpired, ValueError):
        return None
