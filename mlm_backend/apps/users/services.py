import secrets
from urllib.parse import urljoin

from django.conf import settings
from django.db import transaction

from .models import User


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
    while User.objects.filter(referral_code=referral_code).exists():
        referral_code = _random_referral_code()
    base = getattr(settings, "FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/") + "/"
    referral_link = urljoin(base, f"join?ref={referral_code}")
    return member_id, referral_code, referral_link


def resolve_sponsor_by_code(code: str) -> User | None:
    if not code:
        return None
    c = code.strip().upper()
    return User.objects.filter(referral_code__iexact=c).first()
