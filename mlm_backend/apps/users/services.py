import secrets
from urllib.parse import urljoin

from django.conf import settings
from django.db import transaction

from .models import User


def _company_referral_raw() -> str:
    """Read settings at call time so .env-loaded values apply (not stale import-time defaults)."""
    return getattr(settings, "DEFAULT_COMPANY_REFERRAL_CODE", "Admin").strip()


def company_referral_code_normalized() -> str:
    return _company_referral_raw().upper()


def _is_reserved_referral_code(code: str) -> bool:
    return bool(code and code.strip().upper() == _company_referral_raw().upper())


def _random_referral_code() -> str:
    code = secrets.token_urlsafe(6).upper().replace("-", "").replace("_", "")[:8]
    while len(code) < 8:
        code += secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")
    return code[:8]


@transaction.atomic
def allocate_member_identity() -> tuple[str, str, str]:
    last = (
        User.objects.select_for_update()
        .filter(member_id__startswith="MLM")
        .order_by("-member_id")
        .first()
    )
    if last and last.member_id[3:].isdigit():
        next_num = int(last.member_id[3:]) + 1
    else:
        next_num = 1
    member_id = f"MLM{next_num:06d}"
    referral_code = _random_referral_code()
    while _is_reserved_referral_code(referral_code) or User.objects.filter(
        referral_code=referral_code
    ).exists():
        referral_code = _random_referral_code()
    base = getattr(settings, "FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/") + "/"
    referral_link = urljoin(base, f"join?ref={referral_code}")
    return member_id, referral_code, referral_link


def _company_fallback_sponsor() -> User | None:
    """Account used when referral matches DEFAULT_COMPANY_REFERRAL_CODE but no matching member row."""
    u = (
        User.objects.filter(is_superuser=True, is_staff=True)
        .order_by("pk")
        .first()
    )
    if u:
        return u
    return (
        User.objects.filter(is_staff=True, role=User.Role.SUPER_ADMIN)
        .order_by("pk")
        .first()
    )


def resolve_sponsor_by_code(code: str) -> User | None:
    """Resolve sponsor: DB referral_code first; company code falls back to primary admin user."""
    if not code:
        return None
    raw = code.strip()
    by_code = User.objects.filter(referral_code__iexact=raw).first()
    if by_code:
        return by_code
    if raw.upper() == company_referral_code_normalized():
        return _company_fallback_sponsor()
    return None
