from decimal import Decimal

from apps.admin_panel.utils import get_system_config
from apps.courses.models import EBook


def preview_checkout_totals(ebooks: list[EBook]) -> dict[str, str]:
    """Same pricing rules as cart checkout / create-order (excludes sponsor discount)."""
    gateway = Decimal("5.72")
    if not ebooks:
        return {
            "taxable_base": "0.00",
            "gst_amount": "0.00",
            "gateway_charge": str(gateway),
            "total": str(gateway),
        }
    cfg = get_system_config()
    taxable_base = sum((Decimal(str(eb.price))).quantize(Decimal("0.01")) for eb in ebooks).quantize(
        Decimal("0.01")
    )
    gst_rate = Decimal(str(cfg.gst_rate))
    gst = (taxable_base * gst_rate).quantize(Decimal("0.01"))
    total = (taxable_base + gst + gateway).quantize(Decimal("0.01"))
    return {
        "taxable_base": str(taxable_base),
        "gst_amount": str(gst),
        "gateway_charge": str(gateway),
        "total": str(total),
    }
