def mask_aadhaar_display(raw: str) -> str:
    d = "".join(c for c in raw if c.isdigit())
    if len(d) < 4:
        return "XXXX-XXXX-XXXX"
    return f"XXXX-XXXX-{d[-4:]}"
