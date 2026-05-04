import random
import string
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .models import OTPRecord


def generate_otp_code() -> str:
    return "".join(random.choices(string.digits, k=6))


def normalize_otp_code(raw) -> str | None:
    """
    Turn client input into a 6-digit string for DB lookup.

    Leading zeros are lost when OTP is sent as a JSON number; zfill restores them.
    """
    if raw is None:
        return None
    digits = "".join(c for c in str(raw).strip() if c.isdigit())
    if not digits or len(digits) > 6:
        return None
    return digits.zfill(6)


def _otp_send_max() -> int:
    return int(getattr(settings, "OTP_SEND_MAX_PER_WINDOW", 3))


def _otp_send_window_seconds() -> int:
    return int(getattr(settings, "OTP_SEND_WINDOW_SECONDS", 600))


def otp_send_rate_limit_message() -> str:
    max_n = _otp_send_max()
    sec = _otp_send_window_seconds()
    if sec >= 60 and sec % 60 == 0:
        m = sec // 60
        unit = "minute" if m == 1 else "minutes"
        return f"OTP rate limit: max {max_n} per {m} {unit}"
    return f"OTP rate limit: max {max_n} per {sec} seconds"


def can_send_otp(identifier: str) -> bool:
    key = f"otp_send_count:{identifier}"
    n = cache.get(key, 0)
    return n < _otp_send_max()


def register_otp_send(identifier: str):
    key = f"otp_send_count:{identifier}"
    n = cache.get(key, 0)
    cache.set(key, n + 1, timeout=_otp_send_window_seconds())


def create_otp_record(
    *,
    phone=None,
    email=None,
    purpose=OTPRecord.Purpose.LOGIN,
    ip=None,
    payload=None,
    registration_full_name="",
    registration_email=None,
    registration_referral_code="",
    registration_sponsor=None,
):
    code = generate_otp_code()
    expires = timezone.now() + timedelta(minutes=10)
    kwargs = dict(
        phone=phone,
        email=email,
        otp_code=code,
        purpose=purpose,
        expires_at=expires,
        ip_address=ip,
        payload=payload or {},
    )
    if purpose == OTPRecord.Purpose.REGISTER:
        kwargs.update(
            registration_full_name=registration_full_name or "",
            registration_email=registration_email,
            registration_referral_code=registration_referral_code or "",
            registration_sponsor=registration_sponsor,
        )
    return OTPRecord.objects.create(**kwargs)


def verify_otp(phone=None, email=None, code=None, purpose=OTPRecord.Purpose.LOGIN):
    """Resolve OTP by matching code for this identity, not only the latest send row."""
    code_n = normalize_otp_code(code)
    if not code_n:
        return None, "Invalid Otp"

    id_qs = OTPRecord.objects.filter(
        purpose=purpose,
        is_used=False,
        expires_at__gte=timezone.now(),
    )
    if purpose == OTPRecord.Purpose.REGISTER:
        if not phone:
            return None, "phone_required"
        pn = phone.strip() if isinstance(phone, str) else phone
        id_qs = id_qs.filter(phone=pn)
    elif phone:
        pn = phone.strip() if isinstance(phone, str) else phone
        id_qs = id_qs.filter(phone=pn)
    elif email:
        id_qs = id_qs.filter(email=email.strip().lower())
    else:
        return None, "phone_or_email_required"

    rec = id_qs.filter(otp_code=code_n).order_by("-created_at").first()
    if rec:
        if rec.attempts >= 5:
            return None, "Too Many Attempts, Try Again Later"
        rec.is_used = True
        rec.save(update_fields=["is_used"])
        return rec, None

    latest = id_qs.order_by("-created_at").first()
    if latest:
        if latest.attempts >= 5:
            return None, "Too Many Attempts, Try Again Later"
        latest.attempts += 1
        latest.save(update_fields=["attempts"])
    return None, "Invalid Otp"
