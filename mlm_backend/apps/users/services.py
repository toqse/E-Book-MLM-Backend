import secrets
from urllib.parse import urljoin

from django.conf import settings
from django.db import transaction

from .models import User


def _env_company_referral_raw() -> str:
    """Value from DEFAULT_COMPANY_REFERRAL_CODE in settings (typically from env)."""
    return (getattr(settings, "DEFAULT_COMPANY_REFERRAL_CODE", "Admin") or "Admin").strip()


def _stored_company_referral_override() -> str:
    try:
        from apps.admin_panel.utils import get_system_config

        cfg = get_system_config()
        return (getattr(cfg, "default_company_referral_code", None) or "").strip()
    except Exception:
        return ""


def effective_company_referral_code() -> str:
    """Active company referral code: DB override on SystemConfig when set, else env default."""
    override = _stored_company_referral_override()
    if override:
        return override
    return _env_company_referral_raw() or "Admin"


def environment_company_referral_code() -> str:
    """DEFAULT_COMPANY_REFERRAL_CODE from settings only (ignores DB override)."""
    return _env_company_referral_raw() or "Admin"


def company_referral_code_normalized() -> str:
    return effective_company_referral_code().upper()


def _is_reserved_referral_code(code: str) -> bool:
    return bool(code and code.strip().upper() == effective_company_referral_code().upper())


def is_company_referral_signup_code(code: str) -> bool:
    """True when the signup referral code matches the active company default."""
    return _is_reserved_referral_code(code)


def _random_referral_code() -> str:
    code = secrets.token_urlsafe(6).upper().replace("-", "").replace("_", "")[:8]
    while len(code) < 8:
        code += secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")
    return code[:8]


@transaction.atomic
def allocate_member_identity() -> tuple[str, str, str]:
    last = (
        User.objects.select_for_update()
        .filter(member_id__startswith="JST")
        .order_by("-member_id")
        .first()
    )
    if last and last.member_id[3:].isdigit():
        next_num = int(last.member_id[3:]) + 1
    else:
        next_num = 1
    member_id = f"JST{next_num:06d}"
    referral_code = _random_referral_code()
    while _is_reserved_referral_code(referral_code) or User.objects.filter(
        referral_code=referral_code
    ).exists():
        referral_code = _random_referral_code()
    base = getattr(settings, "FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/") + "/"
    referral_link = urljoin(base, f"join?ref={referral_code}")
    return member_id, referral_code, referral_link


def is_account_capped(user) -> bool:
    """True when the member has reached the earning cap and is marked CAPPED."""
    return bool(user) and user.account_status == User.AccountStatus.CAPPED


def maybe_activate_account_on_purchase(user: User) -> bool:
    """
    INACTIVE -> ACTIVE on first qualifying PAID ebook order (same rule as is_book_purchased).
    Mutates user in memory; caller saves. No-op for SUSPENDED, CAPPED, DEACTIVATED, or ACTIVE.
    """
    if user.account_status != User.AccountStatus.INACTIVE:
        return False
    from apps.users.kyc_eligibility import user_has_qualifying_paid_ebook_purchase

    if not user_has_qualifying_paid_ebook_purchase(user):
        return False
    user.account_status = User.AccountStatus.ACTIVE
    return True


def company_fallback_sponsor() -> User | None:
    """Primary admin account used for company-referral fallback and capped-sponsor reassignment."""
    return _company_fallback_sponsor()


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
