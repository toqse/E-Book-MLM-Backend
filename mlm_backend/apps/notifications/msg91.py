"""MSG91 campaign API client for OTP, invoice, and KYC invitation delivery."""

from __future__ import annotations

import logging
import re
from typing import Any

import requests
from django.conf import settings

from apps.admin_panel.utils import get_msg91_authkey

_logger = logging.getLogger(__name__)

_BASE_URL = "https://control.msg91.com/api/v5/campaign/api/campaigns/"
_SLUG_OTP = "email-whatsap-otp"
_SLUG_INVOICE = "email-whatsapp-purchase1"
_SLUG_INVITATION = "email-whatsapp-invitation"
_SLUG_WELCOME_REGISTRATION = "welcome-registration"
_SLUG_KYC_SUBMITTED = "kyc-submitted"
_SLUG_KYC_APPROVED = "kyc-approved"
_SLUG_KYC_REJECTED = "kyc-rejected"
_SLUG_REFERRAL_JOINED = "referral-joined"
_SLUG_MILESTONE_ACHIEVED = "milestone-achieved"
_SLUG_EARNING_CAP_ACHIEVED = "earning-cap-achieved"
_SLUG_WITHDRAWAL_SUBMITTED = "withdrawal-submitted"
_SLUG_WITHDRAWAL_APPROVED = "withdrawal-approved"
_SLUG_WITHDRAWAL_REJECTED = "withdraw-rejected"
_SLUG_REFUND_APPROVED = "refund-approved"
_SLUG_REFUND_REJECTED = "refund-rejected"
_REQUEST_TIMEOUT_SECONDS = 15


def _company_name() -> str:
    return (getattr(settings, "COMPANY_NAME", "") or "Just 200").strip()


def _format_mobile(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", str(phone).strip())
    if not digits:
        return None
    if digits.startswith("+"):
        digits = digits[1:]
    default_cc = (getattr(settings, "MSG91_DEFAULT_COUNTRY_CODE", "91") or "91").strip()
    if default_cc and not digits.startswith(default_cc) and len(digits) == 10:
        digits = f"{default_cc}{digits}"
    return digits


def _text_var(value: str) -> dict[str, str]:
    return {"type": "text", "value": value}


def _body_text_var(*, parameter_name: str, value: str) -> dict[str, str]:
    return {"type": "text", "parameter_name": parameter_name, "value": value}


def _otp_variables(*, otp: str, company_name: str) -> dict[str, Any]:
    return {
        "company_name": {"value": company_name},
        "otp": {"value": otp},
        "body_1": _text_var(otp),
        "button_1": {"type": "text", "subtype": "url", "value": otp},
    }


def _recipient_entry(
    *,
    name: str,
    email: str | None,
    mobile: str | None,
    variables: dict[str, Any],
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name or "Member",
        "mobiles": mobile or "",
        "variables": variables,
    }
    if email:
        entry["email"] = email
    return entry


def _campaign_body(
    *,
    name: str,
    email: str | None,
    mobile: str | None,
    variables: dict[str, Any],
) -> dict[str, Any]:
    return {
        "data": {
            "sendTo": [
                {
                    "to": [
                        _recipient_entry(
                            name=name,
                            email=email,
                            mobile=mobile,
                            variables=variables,
                        )
                    ],
                    "variables": variables,
                }
            ]
        }
    }


def post_campaign(slug: str, body: dict[str, Any]) -> bool:
    authkey = get_msg91_authkey()
    if not authkey:
        _logger.warning("MSG91 authkey not configured; skipping campaign slug=%s", slug)
        return False
    url = f"{_BASE_URL}{slug}/run"
    try:
        resp = requests.post(
            url,
            json=body,
            headers={
                "Content-Type": "application/json",
                "authkey": authkey,
            },
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        if resp.status_code >= 400:
            _logger.warning(
                "MSG91 campaign failed slug=%s status=%s body=%s",
                slug,
                resp.status_code,
                (resp.text or "")[:500],
            )
            return False
        _logger.info("MSG91 campaign sent slug=%s status=%s", slug, resp.status_code)
        return True
    except requests.RequestException:
        _logger.exception("MSG91 campaign request error slug=%s", slug)
        return False


def send_otp_message(
    *,
    name: str,
    email: str | None,
    mobile: str | None,
    otp: str,
) -> bool:
    formatted_mobile = _format_mobile(mobile)
    if not formatted_mobile and not (email or "").strip():
        _logger.warning("MSG91 OTP skipped: no phone or email for name=%s", name)
        return False
    company = _company_name()
    variables = _otp_variables(otp=otp, company_name=company)
    body = _campaign_body(
        name=name,
        email=(email or "").strip() or None,
        mobile=formatted_mobile,
        variables=variables,
    )
    return post_campaign(_SLUG_OTP, body)


def send_invoice_message(
    *,
    name: str,
    email: str | None,
    mobile: str | None,
    invoice_number: str,
    invoice_date: str,
    amount: str,
    invoice_link: str,
) -> bool:
    formatted_mobile = _format_mobile(mobile)
    if not formatted_mobile and not (email or "").strip():
        _logger.warning("MSG91 invoice skipped: no phone or email for name=%s", name)
        return False
    company = _company_name()
    variables: dict[str, Any] = {
        "customer_name": {"value": name},
        "invoice_number": {"value": invoice_number},
        "invoice_date": {"value": invoice_date},
        "amount": {"value": amount},
        "invoice_download_link": {"value": invoice_link},
        "company_name": {"value": company},
        "body_amount": _body_text_var(parameter_name="amount", value=amount),
        "body_customer_name": _body_text_var(parameter_name="customer_name", value=name),
        "body_invoice_url": _body_text_var(parameter_name="invoice_url", value=invoice_link),
        "body_invoice_number": _body_text_var(parameter_name="invoice_number", value=invoice_number),
        "body_invoice_date": _body_text_var(parameter_name="invoice_date", value=invoice_date),
    }
    body = _campaign_body(
        name=name,
        email=(email or "").strip() or None,
        mobile=formatted_mobile,
        variables=variables,
    )
    return post_campaign(_SLUG_INVOICE, body)


def send_invitation_message(
    *,
    name: str,
    email: str | None,
    mobile: str | None,
) -> bool:
    formatted_mobile = _format_mobile(mobile)
    if not formatted_mobile and not (email or "").strip():
        _logger.warning("MSG91 invitation skipped: no phone or email for name=%s", name)
        return False
    company = _company_name()
    variables: dict[str, Any] = {
        "customer_name": {"value": name},
        "company_name": {"value": company},
        "body_customer_name": _body_text_var(parameter_name="customer_name", value=name),
        "body_company_name": _body_text_var(parameter_name="company_name", value=company),
    }
    body = _campaign_body(
        name=name,
        email=(email or "").strip() or None,
        mobile=formatted_mobile,
        variables=variables,
    )
    return post_campaign(_SLUG_INVITATION, body)


def _has_contact(*, email: str | None, mobile: str | None) -> bool:
    return bool(_format_mobile(mobile) or (email or "").strip())


def send_welcome_registration_message(
    *,
    name: str,
    email: str | None,
    mobile: str | None,
) -> bool:
    formatted_mobile = _format_mobile(mobile)
    if not _has_contact(email=email, mobile=mobile):
        _logger.warning("MSG91 welcome skipped: no phone or email for name=%s", name)
        return False
    company = _company_name()
    display_name = name or "Member"
    variables: dict[str, Any] = {
        "member_name": {"value": display_name},
        "company_name": {"value": company},
        "body_1": _text_var(display_name),
    }
    body = _campaign_body(
        name=display_name,
        email=(email or "").strip() or None,
        mobile=formatted_mobile,
        variables=variables,
    )
    return post_campaign(_SLUG_WELCOME_REGISTRATION, body)


def send_kyc_submitted_message(
    *,
    name: str,
    email: str | None,
    mobile: str | None,
    submitted_date: str,
) -> bool:
    formatted_mobile = _format_mobile(mobile)
    if not _has_contact(email=email, mobile=mobile):
        _logger.warning("MSG91 KYC submitted skipped: no phone or email for name=%s", name)
        return False
    company = _company_name()
    display_name = name or "Member"
    variables: dict[str, Any] = {
        "customer_name": {"value": display_name},
        "submitted_date": {"value": submitted_date},
        "company_name": {"value": company},
        "body_1": _text_var(display_name),
        "body_2": _text_var(submitted_date),
    }
    body = _campaign_body(
        name=display_name,
        email=(email or "").strip() or None,
        mobile=formatted_mobile,
        variables=variables,
    )
    return post_campaign(_SLUG_KYC_SUBMITTED, body)


def send_kyc_approved_message(
    *,
    name: str,
    email: str | None,
    mobile: str | None,
) -> bool:
    formatted_mobile = _format_mobile(mobile)
    if not _has_contact(email=email, mobile=mobile):
        _logger.warning("MSG91 KYC approved skipped: no phone or email for name=%s", name)
        return False
    company = _company_name()
    display_name = name or "Member"
    variables: dict[str, Any] = {
        "customer_name": {"value": display_name},
        "company_name": {"value": company},
        "body_1": _text_var(display_name),
    }
    body = _campaign_body(
        name=display_name,
        email=(email or "").strip() or None,
        mobile=formatted_mobile,
        variables=variables,
    )
    return post_campaign(_SLUG_KYC_APPROVED, body)


def send_kyc_rejected_message(
    *,
    name: str,
    email: str | None,
    mobile: str | None,
    rejection_reason: str,
) -> bool:
    formatted_mobile = _format_mobile(mobile)
    if not _has_contact(email=email, mobile=mobile):
        _logger.warning("MSG91 KYC rejected skipped: no phone or email for name=%s", name)
        return False
    company = _company_name()
    display_name = name or "Member"
    reason = (rejection_reason or "").strip() or "Not specified"
    variables: dict[str, Any] = {
        "customer_name": {"value": display_name},
        "rejection_reason": {"value": reason},
        "company_name": {"value": company},
        "body_1": _text_var(display_name),
        "body_2": _text_var(reason),
    }
    body = _campaign_body(
        name=display_name,
        email=(email or "").strip() or None,
        mobile=formatted_mobile,
        variables=variables,
    )
    return post_campaign(_SLUG_KYC_REJECTED, body)


def _amount_str(value) -> str:
    from decimal import Decimal

    if value is None:
        return "0"
    if isinstance(value, Decimal):
        return str(value.quantize(Decimal("0.01")))
    return str(value)


def send_referral_joined_message(
    *,
    name: str,
    email: str | None,
    mobile: str | None,
    referral_name: str,
) -> bool:
    formatted_mobile = _format_mobile(mobile)
    if not _has_contact(email=email, mobile=mobile):
        _logger.warning("MSG91 referral joined skipped: no phone or email for name=%s", name)
        return False
    company = _company_name()
    display_name = name or "Member"
    ref_name = referral_name or "Member"
    variables: dict[str, Any] = {
        "customer_name": {"value": display_name},
        "referral_name": {"value": ref_name},
        "company_name": {"value": company},
        "body_1": _text_var(display_name),
        "body_2": _text_var(ref_name),
    }
    body = _campaign_body(
        name=display_name,
        email=(email or "").strip() or None,
        mobile=formatted_mobile,
        variables=variables,
    )
    return post_campaign(_SLUG_REFERRAL_JOINED, body)


def send_milestone_achieved_message(
    *,
    name: str,
    email: str | None,
    mobile: str | None,
    reward_amount: str,
    milestone_label: str,
) -> bool:
    formatted_mobile = _format_mobile(mobile)
    if not _has_contact(email=email, mobile=mobile):
        _logger.warning("MSG91 milestone achieved skipped: no phone or email for name=%s", name)
        return False
    company = _company_name()
    display_name = name or "Member"
    label = milestone_label or "Milestone"
    amount = reward_amount or "0"
    variables: dict[str, Any] = {
        "customer_name": {"value": display_name},
        "reward_amount": {"value": amount},
        "company_name": {"value": company},
        "body_1": _text_var(display_name),
        "body_2": _text_var(label),
        "body_3": _text_var(amount),
    }
    body = _campaign_body(
        name=display_name,
        email=(email or "").strip() or None,
        mobile=formatted_mobile,
        variables=variables,
    )
    return post_campaign(_SLUG_MILESTONE_ACHIEVED, body)


def send_earning_cap_achieved_message(
    *,
    name: str,
    email: str | None,
    mobile: str | None,
    total_earnings: str,
) -> bool:
    formatted_mobile = _format_mobile(mobile)
    if not _has_contact(email=email, mobile=mobile):
        _logger.warning("MSG91 earning cap skipped: no phone or email for name=%s", name)
        return False
    company = _company_name()
    display_name = name or "Member"
    total = total_earnings or "0"
    variables: dict[str, Any] = {
        "customer_name": {"value": display_name},
        "total_earnings": {"value": total},
        "company_name": {"value": company},
        "body_1": _text_var(display_name),
        "body_2": _text_var(total),
    }
    body = _campaign_body(
        name=display_name,
        email=(email or "").strip() or None,
        mobile=formatted_mobile,
        variables=variables,
    )
    return post_campaign(_SLUG_EARNING_CAP_ACHIEVED, body)


def send_withdrawal_submitted_message(
    *,
    name: str,
    email: str | None,
    mobile: str | None,
    amount: str,
    request_date: str,
) -> bool:
    formatted_mobile = _format_mobile(mobile)
    if not _has_contact(email=email, mobile=mobile):
        _logger.warning("MSG91 withdrawal submitted skipped: no phone or email for name=%s", name)
        return False
    company = _company_name()
    display_name = name or "Member"
    amt = amount or "0"
    date_str = request_date or ""
    variables: dict[str, Any] = {
        "customer_name": {"value": display_name},
        "amount": {"value": amt},
        "request_date": {"value": date_str},
        "company_name": {"value": company},
        "body_1": _text_var(display_name),
        "body_2": _text_var(amt),
        "body_3": _text_var(date_str),
    }
    body = _campaign_body(
        name=display_name,
        email=(email or "").strip() or None,
        mobile=formatted_mobile,
        variables=variables,
    )
    return post_campaign(_SLUG_WITHDRAWAL_SUBMITTED, body)


def send_withdrawal_approved_message(
    *,
    name: str,
    email: str | None,
    mobile: str | None,
    amount: str,
) -> bool:
    formatted_mobile = _format_mobile(mobile)
    if not _has_contact(email=email, mobile=mobile):
        _logger.warning("MSG91 withdrawal approved skipped: no phone or email for name=%s", name)
        return False
    company = _company_name()
    display_name = name or "Member"
    amt = amount or "0"
    variables: dict[str, Any] = {
        "customer_name": {"value": display_name},
        "amount": {"value": amt},
        "company_name": {"value": company},
        "body_1": _text_var(display_name),
        "body_2": _text_var(amt),
    }
    body = _campaign_body(
        name=display_name,
        email=(email or "").strip() or None,
        mobile=formatted_mobile,
        variables=variables,
    )
    return post_campaign(_SLUG_WITHDRAWAL_APPROVED, body)


def send_withdrawal_rejected_message(
    *,
    name: str,
    email: str | None,
    mobile: str | None,
    rejection_reason: str,
) -> bool:
    formatted_mobile = _format_mobile(mobile)
    if not _has_contact(email=email, mobile=mobile):
        _logger.warning("MSG91 withdrawal rejected skipped: no phone or email for name=%s", name)
        return False
    company = _company_name()
    display_name = name or "Member"
    reason = (rejection_reason or "").strip() or "Not specified"
    variables: dict[str, Any] = {
        "customer_name": {"value": display_name},
        "rejection_reason": {"value": reason},
        "company_name": {"value": company},
        "body_1": _text_var(display_name),
        "body_2": _text_var(reason),
    }
    body = _campaign_body(
        name=display_name,
        email=(email or "").strip() or None,
        mobile=formatted_mobile,
        variables=variables,
    )
    return post_campaign(_SLUG_WITHDRAWAL_REJECTED, body)


def send_refund_approved_message(
    *,
    name: str,
    email: str | None,
    mobile: str | None,
    refund_amount: str,
) -> bool:
    formatted_mobile = _format_mobile(mobile)
    if not _has_contact(email=email, mobile=mobile):
        _logger.warning("MSG91 refund approved skipped: no phone or email for name=%s", name)
        return False
    company = _company_name()
    display_name = name or "Member"
    amt = refund_amount or "0"
    variables: dict[str, Any] = {
        "customer_name": {"value": display_name},
        "refund_amount": {"value": amt},
        "company_name": {"value": company},
        "body_1": _text_var(display_name),
        "body_2": _text_var(amt),
    }
    body = _campaign_body(
        name=display_name,
        email=(email or "").strip() or None,
        mobile=formatted_mobile,
        variables=variables,
    )
    return post_campaign(_SLUG_REFUND_APPROVED, body)


def send_refund_rejected_message(
    *,
    name: str,
    email: str | None,
    mobile: str | None,
    rejection_reason: str,
) -> bool:
    formatted_mobile = _format_mobile(mobile)
    if not _has_contact(email=email, mobile=mobile):
        _logger.warning("MSG91 refund rejected skipped: no phone or email for name=%s", name)
        return False
    company = _company_name()
    display_name = name or "Member"
    reason = (rejection_reason or "").strip() or "Not specified"
    variables: dict[str, Any] = {
        "customer_name": {"value": display_name},
        "rejection_reason": {"value": reason},
        "company_name": {"value": company},
        "body_1": _text_var(display_name),
        "body_2": _text_var(reason),
    }
    body = _campaign_body(
        name=display_name,
        email=(email or "").strip() or None,
        mobile=formatted_mobile,
        variables=variables,
    )
    return post_campaign(_SLUG_REFUND_REJECTED, body)
