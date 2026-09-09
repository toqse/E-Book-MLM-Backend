from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from apps.users.models import User
from apps.users.services import is_account_capped, resolve_sponsor_by_code

from .models import StoreReferralLead

STORE_REFERRAL_LEAD_TTL_DAYS = 30


def _lead_expires_at():
    return timezone.now() + timedelta(days=STORE_REFERRAL_LEAD_TTL_DAYS)


def _normalize_platform(raw: str) -> str | None:
    value = (raw or "").strip().lower()
    if value in ("android", "play_store", "playstore"):
        return StoreReferralLead.Platform.ANDROID
    if value in ("ios", "app_store", "appstore", "apple"):
        return StoreReferralLead.Platform.IOS
    return None


def validate_store_referral_code(referral_code: str) -> tuple[bool, str | None]:
    code = (referral_code or "").strip()
    if not code:
        return False, "Referral code is required."
    sponsor = resolve_sponsor_by_code(code)
    if not sponsor:
        return False, "Invalid referral code"
    if is_account_capped(sponsor):
        return False, "This referral link is no longer active"
    return True, None


def upsert_store_referral_lead(*, phone: str, referral_code: str, platform: str) -> tuple[bool, str | None]:
    if User.objects.filter(phone=phone).exists():
        return False, "User already exists"
    platform_value = _normalize_platform(platform)
    if not platform_value:
        return False, "Invalid platform"
    ok, err = validate_store_referral_code(referral_code)
    if not ok:
        return False, err
    code = referral_code.strip().upper()
    expires_at = _lead_expires_at()
    StoreReferralLead.objects.update_or_create(
        phone=phone,
        defaults={
            "referral_code": code,
            "platform": platform_value,
            "expires_at": expires_at,
        },
    )
    return True, None


def _empty_referral_result(phone_registered: bool) -> dict[str, str | bool | None]:
    return {
        "referral_code": None,
        "referrer_name": None,
        "referrer_id": None,
        "phone_registered": phone_registered,
    }


def lookup_referral_by_phone(phone: str) -> dict[str, str | bool | None]:
    if User.objects.filter(phone=phone).exists():
        return _empty_referral_result(phone_registered=True)
    lead = StoreReferralLead.objects.filter(phone=phone).first()
    if not lead:
        return _empty_referral_result(phone_registered=False)
    if lead.expires_at <= timezone.now():
        lead.delete()
        return _empty_referral_result(phone_registered=False)
    referrer = resolve_sponsor_by_code(lead.referral_code or "")
    return {
        "referral_code": lead.referral_code or None,
        "referrer_name": (referrer.full_name or None) if referrer else None,
        "referrer_id": (referrer.member_id or None) if referrer else None,
        "phone_registered": False,
    }


def lookup_referral_code_by_phone(phone: str) -> str | None:
    result = lookup_referral_by_phone(phone)
    if result.get("phone_registered"):
        return None
    code = result.get("referral_code")
    return code if isinstance(code, str) else None


def consume_store_referral_lead(phone: str) -> None:
    StoreReferralLead.objects.filter(phone=phone).delete()
