"""
One-off data cleanup for PAN/Aadhaar uniqueness before DB constraints are applied.

Used by agreements and users migrations; safe to import from RunPython only.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
AADHAAR_RE = re.compile(r"^\d{12}$")

KYC_PENDING = "PENDING"


def _norm_pan(value: str | None) -> str | None:
    u = (value or "").strip().upper()
    return u if u and PAN_RE.match(u) else None


def _norm_aadhaar(value: str | None) -> str | None:
    d = "".join(c for c in (value or "") if c.isdigit())
    return d if d and AADHAAR_RE.match(d) else None


def _is_masked_aadhaar(value: str | None) -> bool:
    raw = (value or "").strip().upper()
    return raw.startswith("XXXX-")


def _reset_user_kyc(user) -> None:
    user.kyc_status = KYC_PENDING
    user.kyc_submitted_at = None
    user.kyc_reviewed_at = None
    user.kyc_rejection_reason = ""


def backfill_and_normalize(apps) -> None:
    Profile = apps.get_model("agreements", "MemberComplianceProfile")
    User = apps.get_model("users", "User")

    profiles = list(Profile.objects.select_related("user").all())
    profile_updates: list[Any] = []
    user_updates: dict[int, Any] = {}

    for profile in profiles:
        changed = False
        pan = _norm_pan(profile.pan_number)
        if pan and profile.pan_number != pan:
            profile.pan_number = pan
            changed = True
        elif profile.pan_number and not pan:
            profile.pan_number = ""
            changed = True

        aad = _norm_aadhaar(profile.aadhar_number)
        if aad and profile.aadhar_number != aad:
            profile.aadhar_number = aad
            changed = True
        elif profile.aadhar_number and not aad:
            profile.aadhar_number = ""
            changed = True

        if changed:
            profile_updates.append(profile)

        user = profile.user
        if user is None:
            continue
        user_changed = False
        if pan and user.pan_number != pan:
            user.pan_number = pan
            user_changed = True
        if aad and user.aadhaar_number != aad:
            user.aadhaar_number = aad
            user_changed = True
        elif _is_masked_aadhaar(user.aadhaar_number) and not aad:
            user.aadhaar_number = None
            user_changed = True
        if user_changed:
            user_updates[user.pk] = user

    if profile_updates:
        Profile.objects.bulk_update(
            profile_updates, ["pan_number", "aadhar_number"], batch_size=500
        )
    if user_updates:
        User.objects.bulk_update(
            list(user_updates.values()),
            ["pan_number", "aadhaar_number"],
            batch_size=500,
        )

    for user in User.objects.exclude(aadhaar_number__isnull=True).exclude(
        aadhaar_number=""
    ):
        if _is_masked_aadhaar(user.aadhaar_number):
            user.aadhaar_number = None
            user.save(update_fields=["aadhaar_number"])

    for user in User.objects.exclude(pan_number__isnull=True).exclude(pan_number=""):
        pan = _norm_pan(user.pan_number)
        if pan and user.pan_number != pan:
            user.pan_number = pan
            user.save(update_fields=["pan_number"])
        elif user.pan_number and not pan:
            user.pan_number = None
            user.save(update_fields=["pan_number"])


def dedupe_profiles(apps) -> None:
    Profile = apps.get_model("agreements", "MemberComplianceProfile")
    User = apps.get_model("users", "User")

    profiles = list(Profile.objects.select_related("user").order_by("updated_at", "user_id"))

    for field_name, norm_fn, user_field in (
        ("pan_number", _norm_pan, "pan_number"),
        ("aadhar_number", _norm_aadhaar, "aadhaar_number"),
    ):
        groups: dict[str, list] = defaultdict(list)
        for profile in profiles:
            key = norm_fn(getattr(profile, field_name))
            if key:
                groups[key].append(profile)

        loser_profiles: list[Any] = []
        loser_users: dict[int, Any] = {}

        for _key, group in groups.items():
            if len(group) <= 1:
                continue
            group.sort(key=lambda p: (p.updated_at, p.user_id))
            for loser in group[1:]:
                setattr(loser, field_name, "")
                loser_profiles.append(loser)
                user = loser.user
                if user is None:
                    continue
                setattr(user, user_field, None)
                _reset_user_kyc(user)
                loser_users[user.pk] = user

        if loser_profiles:
            Profile.objects.bulk_update(
                loser_profiles, [field_name], batch_size=500
            )
        if loser_users:
            User.objects.bulk_update(
                list(loser_users.values()),
                [
                    user_field,
                    "kyc_status",
                    "kyc_submitted_at",
                    "kyc_reviewed_at",
                    "kyc_rejection_reason",
                ],
                batch_size=500,
            )


def dedupe_users_without_profile(apps) -> None:
    User = apps.get_model("users", "User")

    for field_name, norm_fn in (("pan_number", _norm_pan), ("aadhaar_number", _norm_aadhaar)):
        users = list(
            User.objects.exclude(**{f"{field_name}__isnull": True})
            .exclude(**{field_name: ""})
            .order_by("created_at", "id")
        )
        groups: dict[str, list] = defaultdict(list)
        for user in users:
            key = norm_fn(getattr(user, field_name))
            if key:
                groups[key].append(user)

        losers: list[Any] = []
        for _key, group in groups.items():
            if len(group) <= 1:
                continue
            for loser in group[1:]:
                setattr(loser, field_name, None)
                _reset_user_kyc(loser)
                losers.append(loser)

        if losers:
            User.objects.bulk_update(
                losers,
                [
                    field_name,
                    "kyc_status",
                    "kyc_submitted_at",
                    "kyc_reviewed_at",
                    "kyc_rejection_reason",
                ],
                batch_size=500,
            )


def run_identity_data_cleanup(apps) -> None:
    backfill_and_normalize(apps)
    dedupe_profiles(apps)
    dedupe_users_without_profile(apps)
