"""Signed, time-limited tokens so acceptance-proof PDF URLs work in a browser (no Bearer header)."""

from __future__ import annotations

import uuid
from typing import Optional

from django.core import signing

# Distinct salt so this token cannot be reused for other signing.dumps callers.
SALT = "agreement.acceptance_proof.download_v1"
# Links from GET /agreements/ remain valid for one week (refresh by calling agreements again).
MAX_AGE_SECONDS = 60 * 60 * 24 * 7


def build_proof_download_token(*, user_id: int, acceptance_batch_id: uuid.UUID) -> str:
    return signing.dumps(
        {"uid": user_id, "bid": str(acceptance_batch_id)},
        salt=SALT,
        compress=True,
    )


def parse_proof_download_token(token: str) -> Optional[tuple[int, uuid.UUID]]:
    try:
        data = signing.loads(token, salt=SALT, max_age=MAX_AGE_SECONDS)
    except signing.BadSignature:
        return None
    try:
        uid = int(data["uid"])
        bid = uuid.UUID(str(data["bid"]))
    except (KeyError, ValueError, TypeError):
        return None
    return uid, bid
