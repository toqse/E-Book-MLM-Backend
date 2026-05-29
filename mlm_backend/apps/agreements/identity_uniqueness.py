"""PAN and Aadhaar uniqueness checks across MemberComplianceProfile and User."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from django.db.models import Q

from apps.agreements.models import MemberComplianceProfile
from apps.users.models import User

if TYPE_CHECKING:
    from django.db.models import QuerySet

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
AADHAAR_RE = re.compile(r"^\d{12}$")


def normalize_pan(value: str | None) -> str | None:
    u = (value or "").strip().upper()
    if not u:
        return None
    return u if PAN_RE.match(u) else u


def normalize_aadhaar(value: str | None) -> str | None:
    d = "".join(c for c in (value or "") if c.isdigit())
    if not d:
        return None
    return d if AADHAAR_RE.match(d) else None


def is_masked_aadhaar_display(value: str | None) -> bool:
    raw = (value or "").strip().upper()
    return raw.startswith("XXXX-") or raw.startswith("XXXX")


def _profile_pan_qs(pan: str, *, exclude_user_id: int | None) -> QuerySet:
    qs = MemberComplianceProfile.objects.filter(pan_number__iexact=pan).exclude(
        pan_number=""
    )
    if exclude_user_id is not None:
        qs = qs.exclude(user_id=exclude_user_id)
    return qs


def _profile_aadhaar_qs(aadhaar: str, *, exclude_user_id: int | None) -> QuerySet:
    qs = MemberComplianceProfile.objects.filter(aadhar_number=aadhaar).exclude(
        aadhar_number=""
    )
    if exclude_user_id is not None:
        qs = qs.exclude(user_id=exclude_user_id)
    return qs


def _user_pan_qs(pan: str, *, exclude_user_id: int | None) -> QuerySet:
    qs = User.objects.filter(pan_number__iexact=pan).exclude(
        Q(pan_number__isnull=True) | Q(pan_number="")
    )
    if exclude_user_id is not None:
        qs = qs.exclude(pk=exclude_user_id)
    return qs


def _user_aadhaar_qs(aadhaar: str, *, exclude_user_id: int | None) -> QuerySet:
    qs = User.objects.filter(aadhaar_number=aadhaar).exclude(
        Q(aadhaar_number__isnull=True) | Q(aadhaar_number="")
    )
    if exclude_user_id is not None:
        qs = qs.exclude(pk=exclude_user_id)
    return qs


def pan_taken_by_other_user(pan: str | None, *, exclude_user_id: int | None) -> bool:
    normalized = normalize_pan(pan)
    if not normalized:
        return False
    if _profile_pan_qs(normalized, exclude_user_id=exclude_user_id).exists():
        return True
    return _user_pan_qs(normalized, exclude_user_id=exclude_user_id).exists()


def aadhaar_taken_by_other_user(
    aadhaar: str | None, *, exclude_user_id: int | None
) -> bool:
    normalized = normalize_aadhaar(aadhaar)
    if not normalized:
        return False
    if _profile_aadhaar_qs(normalized, exclude_user_id=exclude_user_id).exists():
        return True
    return _user_aadhaar_qs(normalized, exclude_user_id=exclude_user_id).exists()


def validate_identity_uniqueness_for_user(
    *,
    pan: str | None,
    aadhaar: str | None,
    user_id: int,
) -> dict[str, str]:
    """Return field-name -> error message for collisions with other users."""
    errors: dict[str, str] = {}
    if pan_taken_by_other_user(pan, exclude_user_id=user_id):
        errors["pan_number"] = "This PAN is already linked to another account."
    if aadhaar_taken_by_other_user(aadhaar, exclude_user_id=user_id):
        errors["aadhar_number"] = "This Aadhaar is already linked to another account."
    return errors
