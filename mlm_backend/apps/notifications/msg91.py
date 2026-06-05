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
_SLUG_INVOICE = "email-whatsapp-purchase"
_SLUG_INVITATION = "email-whatsapp-invitation"
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


def _image_var(value: str = "text") -> dict[str, str]:
    return {"type": "image", "value": value}


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
        "header_1": _image_var(),
        "body_1": _text_var(name),
        "body_2": _text_var(invoice_number),
        "body_3": _text_var(invoice_date),
        "body_4": _text_var(amount),
        "body_5": _text_var(invoice_link),
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
        "header_1": _image_var(),
        "body_1": _text_var(name),
        "body_2": _text_var(company),
    }
    body = _campaign_body(
        name=name,
        email=(email or "").strip() or None,
        mobile=formatted_mobile,
        variables=variables,
    )
    return post_campaign(_SLUG_INVITATION, body)
