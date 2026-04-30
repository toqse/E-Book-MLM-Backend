import re

# E.164: + followed by ITU-T max 15 digits (first digit never 0).
REGISTER_PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")

# India mobile (+91): 10-digit national number; first digit 6–9 (common mobile numbering).
INDIA_MOBILE_E164_RE = re.compile(r"^\+91[6-9]\d{9}$")


def normalize_phone_registration(raw: str) -> str:
    """Require international form with explicit leading '+' (e.g. +918589960592)."""
    if not raw or not isinstance(raw, str):
        raise ValueError("Phone is required")
    s = raw.strip().replace(" ", "").replace("-", "")
    if not REGISTER_PHONE_RE.match(s):
        raise ValueError("Phone must be with country code")
    if s.startswith("+91") and not INDIA_MOBILE_E164_RE.match(s):
        raise ValueError(
            "Invalid Mobile Number"
        )
    return s
