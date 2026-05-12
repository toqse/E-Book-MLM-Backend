"""Member-specific documents bundled with GET /api/v1/agreements/."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlencode

from django.urls import reverse

from apps.agreements.proof_download_token import build_proof_download_token
from apps.agreements.proof_service import ensure_proof_for_batch, latest_acceptance_batch_id
from apps.payments.models import Order

if TYPE_CHECKING:
    from django.http import HttpRequest

    from apps.users.models import User


def build_agreements_user_array(request: HttpRequest, user: User) -> list[dict]:
    """One element: current member with paid order invoices + latest acceptance proof."""
    invoices: list[dict] = []
    for order in (
        Order.objects.filter(user=user, status=Order.Status.PAID)
        .select_related("gst_invoice")
        .order_by("-paid_at", "-id")
    ):
        inv = getattr(order, "gst_invoice", None)
        pdf_url = None
        if inv and inv.pdf_file:
            name = (getattr(inv.pdf_file, "name", None) or "").strip()
            if name:
                try:
                    pdf_url = request.build_absolute_uri(inv.pdf_file.url)
                except Exception:
                    pdf_url = inv.pdf_file.url
        invoices.append(
            {
                "order_id": order.id,
                "order_number": order.order_number,
                "invoice_number": (inv.invoice_number if inv else None) or order.gst_invoice_number or None,
                "invoice_pdf_url": pdf_url,
                "paid_at": order.paid_at.isoformat() if order.paid_at else None,
            }
        )

    proof_payload: dict | None = None
    batch = latest_acceptance_batch_id(user)
    if batch is not None:
        proof = ensure_proof_for_batch(user, batch)
        if proof:
            download_path = reverse(
                "agreement_acceptance_proof_download",
                kwargs={"acceptance_batch_id": batch},
            )
            base_url = request.build_absolute_uri(download_path)
            dl_token = build_proof_download_token(user_id=user.id, acceptance_batch_id=batch)
            proof_payload = {
                "acceptance_batch_id": str(batch),
                "pdf_download_url": f"{base_url}?{urlencode({'token': dl_token})}",
                "issued_at": proof.issued_at.isoformat() if proof.issued_at else None,
                "verification": {
                    "signature": proof.signature_hex,
                    "algo": "HMAC-SHA256",
                },
            }

    return [
        {
            "id": user.id,
            "order_invoices": invoices,
            "compliance_acceptance_proof": proof_payload,
        }
    ]
