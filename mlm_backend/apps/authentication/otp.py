import random
import string
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from .models import OTPRecord


def generate_otp_code() -> str:
    return "".join(random.choices(string.digits, k=6))


def can_send_otp(identifier: str) -> bool:
    key = f"otp_send_count:{identifier}"
    n = cache.get(key, 0)
    return n < 3


def register_otp_send(identifier: str):
    key = f"otp_send_count:{identifier}"
    n = cache.get(key, 0)
    cache.set(key, n + 1, timeout=600)


def create_otp_record(*, phone=None, email=None, purpose=OTPRecord.Purpose.LOGIN, ip=None):
    code = generate_otp_code()
    expires = timezone.now() + timedelta(minutes=10)
    return OTPRecord.objects.create(
        phone=phone,
        email=email,
        otp_code=code,
        purpose=purpose,
        expires_at=expires,
        ip_address=ip,
    )


def verify_otp(phone=None, email=None, code=None, purpose=OTPRecord.Purpose.LOGIN):
    qs = OTPRecord.objects.filter(
        purpose=purpose, is_used=False, expires_at__gte=timezone.now()
    )
    if phone:
        qs = qs.filter(phone=phone)
    elif email:
        qs = qs.filter(email=email)
    else:
        return None, "phone_or_email_required"
    rec = qs.order_by("-created_at").first()
    if not rec:
        return None, "invalid_otp"
    if rec.attempts >= 5:
        return None, "too_many_attempts"
    if rec.otp_code != code:
        rec.attempts += 1
        rec.save(update_fields=["attempts"])
        return None, "invalid_otp"
    rec.is_used = True
    rec.save(update_fields=["is_used"])
    return rec, None
