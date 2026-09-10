"""MSG91 campaign API client for OTP, invoice, KYC invitation, and lifecycle delivery.

Variable keys use MSG91 namespaced template IDs so email and WhatsApp channels
in each campaign receive the correct fields (e.g. invoice_template_10:amount,
purchase_confirmed:body_amount).
"""

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
    # Email template: template_22_05_2026_12_05_3 | WhatsApp: otp_verification
    return {
        "template_22_05_2026_12_05_3:company_name": {"value": company_name},
        "template_22_05_2026_12_05_3:otp": {"value": otp},
        "otp_verification:body_1": _text_var(otp),
        "otp_verification:button_1": {"type": "text", "subtype": "url", "value": otp},
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
    # Email: invoice_template_10 | WhatsApp: purchase_confirmed (no WA invoice URL)
    variables: dict[str, Any] = {
        "invoice_template_10:customer_name": {"value": name},
        "invoice_template_10:invoice_number": {"value": invoice_number},
        "invoice_template_10:invoice_date": {"value": invoice_date},
        "invoice_template_10:amount": {"value": amount},
        "invoice_template_10:invoice_download_link": {"value": invoice_link},
        "invoice_template_10:company_name": {"value": company},
        "purchase_confirmed:body_customer_name": _body_text_var(
            parameter_name="customer_name", value=name
        ),
        "purchase_confirmed:body_invoice_number": _body_text_var(
            parameter_name="invoice_number", value=invoice_number
        ),
        "purchase_confirmed:body_invoice_date": _body_text_var(
            parameter_name="invoice_date", value=invoice_date
        ),
        "purchase_confirmed:body_amount": _body_text_var(
            parameter_name="amount", value=amount
        ),
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
    # Email: invitation_55 | WhatsApp: distributor_invitation
    variables: dict[str, Any] = {
        "invitation_55:customer_name": {"value": name},
        "invitation_55:company_name": {"value": company},
        "distributor_invitation:body_customer_name": _body_text_var(
            parameter_name="customer_name", value=name
        ),
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
    # Email: registration_success_ | WhatsApp: registration_success
    variables: dict[str, Any] = {
        "registration_success_:member_name": {"value": display_name},
        "registration_success_:company_name": {"value": company},
        "registration_success:body_member_name": _body_text_var(
            parameter_name="member_name", value=display_name
        ),
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
    # Email: kyc_submitted_4 | WhatsApp: kyc_submitted
    variables: dict[str, Any] = {
        "kyc_submitted_4:customer_name": {"value": display_name},
        "kyc_submitted_4:submitted_date": {"value": submitted_date},
        "kyc_submitted_4:company_name": {"value": company},
        "kyc_submitted:body_customer_name": _body_text_var(
            parameter_name="customer_name", value=display_name
        ),
        "kyc_submitted:body_submitted_date": _body_text_var(
            parameter_name="submitted_date", value=submitted_date
        ),
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
    # Email: kyc_approved_10 | WhatsApp: kyc_approved
    variables: dict[str, Any] = {
        "kyc_approved_10:customer_name": {"value": display_name},
        "kyc_approved_10:company_name": {"value": company},
        "kyc_approved:body_customer_name": _body_text_var(
            parameter_name="customer_name", value=display_name
        ),
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
    # Email: kyc_rejected_10 | WhatsApp: kyc_rejected
    variables: dict[str, Any] = {
        "kyc_rejected_10:customer_name": {"value": display_name},
        "kyc_rejected_10:rejection_reason": {"value": reason},
        "kyc_rejected_10:company_name": {"value": company},
        "kyc_rejected:body_customer_name": _body_text_var(
            parameter_name="customer_name", value=display_name
        ),
        "kyc_rejected:body_rejection_reason": _body_text_var(
            parameter_name="rejection_reason", value=reason
        ),
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
    # Email + WhatsApp: referral_joined
    variables: dict[str, Any] = {
        "referral_joined:customer_name": {"value": display_name},
        "referral_joined:referral_name": {"value": ref_name},
        "referral_joined:company_name": {"value": company},
        "referral_joined:body_customer_name": _body_text_var(
            parameter_name="customer_name", value=display_name
        ),
        "referral_joined:body_referral_name": _body_text_var(
            parameter_name="referral_name", value=ref_name
        ),
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
    amount = reward_amount or "0"
    _ = milestone_label  # retained for callers; new MSG91 template omits label
    # Email: milestone_achieved_3 | WhatsApp: milestone_achieved_utility
    # New MSG91 template does not include milestone_label.
    variables: dict[str, Any] = {
        "milestone_achieved_3:customer_name": {"value": display_name},
        "milestone_achieved_3:reward_amount": {"value": amount},
        "milestone_achieved_3:company_name": {"value": company},
        "milestone_achieved_utility:body_customer_name": _body_text_var(
            parameter_name="customer_name", value=display_name
        ),
        "milestone_achieved_utility:body_reward_amount": _body_text_var(
            parameter_name="reward_amount", value=amount
        ),
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
    # Email + WhatsApp: earning_cap_achieved
    variables: dict[str, Any] = {
        "earning_cap_achieved:customer_name": {"value": display_name},
        "earning_cap_achieved:total_earnings": {"value": total},
        "earning_cap_achieved:company_name": {"value": company},
        "earning_cap_achieved:body_customer_name": _body_text_var(
            parameter_name="customer_name", value=display_name
        ),
        "earning_cap_achieved:body_total_earnings": _body_text_var(
            parameter_name="total_earnings", value=total
        ),
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
    # Email + WhatsApp: withdrawal_submitted
    variables: dict[str, Any] = {
        "withdrawal_submitted:customer_name": {"value": display_name},
        "withdrawal_submitted:amount": {"value": amt},
        "withdrawal_submitted:request_date": {"value": date_str},
        "withdrawal_submitted:company_name": {"value": company},
        "withdrawal_submitted:body_customer_name": _body_text_var(
            parameter_name="customer_name", value=display_name
        ),
        "withdrawal_submitted:body_amount": _body_text_var(
            parameter_name="amount", value=amt
        ),
        "withdrawal_submitted:body_request_date": _body_text_var(
            parameter_name="request_date", value=date_str
        ),
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
    # Email: withdrawal_approved_3 | WhatsApp: withdrawal_approved
    variables: dict[str, Any] = {
        "withdrawal_approved_3:customer_name": {"value": display_name},
        "withdrawal_approved_3:amount": {"value": amt},
        "withdrawal_approved_3:company_name": {"value": company},
        "withdrawal_approved:body_customer_name": _body_text_var(
            parameter_name="customer_name", value=display_name
        ),
        "withdrawal_approved:body_amount": _body_text_var(
            parameter_name="amount", value=amt
        ),
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
    # Email: withdrawal_rejected_2 | WhatsApp: withdrawal_rejected
    variables: dict[str, Any] = {
        "withdrawal_rejected_2:customer_name": {"value": display_name},
        "withdrawal_rejected_2:rejection_reason": {"value": reason},
        "withdrawal_rejected_2:company_name": {"value": company},
        "withdrawal_rejected:body_customer_name": _body_text_var(
            parameter_name="customer_name", value=display_name
        ),
        "withdrawal_rejected:body_rejection_reason": _body_text_var(
            parameter_name="rejection_reason", value=reason
        ),
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
    # Email: refund_approved_2 | WhatsApp: refund_approved
    variables: dict[str, Any] = {
        "refund_approved_2:customer_name": {"value": display_name},
        "refund_approved_2:refund_amount": {"value": amt},
        "refund_approved_2:company_name": {"value": company},
        "refund_approved:body_customer_name": _body_text_var(
            parameter_name="customer_name", value=display_name
        ),
        "refund_approved:body_refund_amount": _body_text_var(
            parameter_name="refund_amount", value=amt
        ),
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
    # Email + WhatsApp: refund_rejected
    variables: dict[str, Any] = {
        "refund_rejected:customer_name": {"value": display_name},
        "refund_rejected:rejection_reason": {"value": reason},
        "refund_rejected:company_name": {"value": company},
        "refund_rejected:body_customer_name": _body_text_var(
            parameter_name="customer_name", value=display_name
        ),
        "refund_rejected:body_rejection_reason": _body_text_var(
            parameter_name="rejection_reason", value=reason
        ),
    }
    body = _campaign_body(
        name=display_name,
        email=(email or "").strip() or None,
        mobile=formatted_mobile,
        variables=variables,
    )
    return post_campaign(_SLUG_REFUND_REJECTED, body)
