"""Signed, time-limited tokens for post-refund KYC invitation deep links."""

from __future__ import annotations

from typing import Optional

from django.conf import settings
from django.core import signing

SALT = "kyc.invite_v1"


def _max_age_seconds() -> int:
    days = int(getattr(settings, "KYC_INVITE_TOKEN_MAX_AGE_DAYS", 30))
    return max(1, days) * 24 * 60 * 60


def build_kyc_invite_token(*, user_id: int) -> str:
    return signing.dumps({"uid": int(user_id)}, salt=SALT, compress=True)


def parse_kyc_invite_token(token: str) -> Optional[int]:
    try:
        data = signing.loads((token or "").strip(), salt=SALT, max_age=_max_age_seconds())
    except signing.BadSignature:
        return None
    try:
        return int(data["uid"])
    except (KeyError, TypeError, ValueError):
        return None
