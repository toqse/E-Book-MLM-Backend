"""Generate and persist per-batch agreement acceptance proof PDFs + HMAC signatures."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Optional
from uuid import UUID

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.agreements.models import (
    UserAgreementAcceptance,
    UserAgreementAcceptanceDeclaration,
    UserAgreementAcceptanceProof,
)
from apps.agreements.proof_pdf import build_acceptance_proof_pdf_bytes
from apps.users.models import User


def _signing_secret() -> bytes:
    raw = (getattr(settings, "AGREEMENT_PROOF_SIGNING_SECRET", None) or "").strip()
    if not raw:
        raw = settings.SECRET_KEY
    return raw.encode("utf-8")


def canonical_signing_message_legacy_v1(
    *,
    user_id: int,
    acceptance_batch_id: str,
    documents: list[dict],
    accepted_at_iso: str,
    accepted_ips_joined: str,
) -> bytes:
    """Original payload without declaration (existing proofs / backward compat)."""
    body = {
        "v": 1,
        "user_id": user_id,
        "acceptance_batch_id": acceptance_batch_id,
        "documents": documents,
        "accepted_at": accepted_at_iso,
        "accepted_ips": accepted_ips_joined,
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_signing_message_v2(
    *,
    user_id: int,
    acceptance_batch_id: str,
    documents: list[dict],
    accepted_at_iso: str,
    accepted_ips_joined: str,
    declaration: str,
) -> bytes:
    body = {
        "v": 2,
        "acceptance_batch_id": acceptance_batch_id,
        "accepted_at": accepted_at_iso,
        "accepted_ips": accepted_ips_joined,
        "declaration": declaration,
        "documents": documents,
        "user_id": user_id,
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _format_signature_datetime(dt) -> str:
    """e.g. 2020.09.08 13:18:17 +05:30"""
    base = dt.strftime("%Y.%m.%d %H:%M:%S")
    z = dt.strftime("%z")
    if len(z) == 5:
        z = z[:3] + ":" + z[3:]
    return f"{base} {z}".strip() if z else base


def compute_hmac_hex(message: bytes) -> str:
    return hmac.new(_signing_secret(), message, hashlib.sha256).hexdigest()


def verify_hmac_for_batch(*, user_id: int, acceptance_batch_id: str, signature_hex: str) -> bool:
    """Recompute HMAC from current DB rows for this user+batch (for tests / tooling)."""
    rows = list(
        UserAgreementAcceptance.objects.filter(
            user_id=user_id,
            acceptance_batch_id=acceptance_batch_id,
        )
        .select_related("document")
        .order_by("document_id")
    )
    if not rows:
        return False
    docs = [{"id": r.document_id, "name": r.document.name, "version": r.version_accepted} for r in rows]
    issued = min(r.accepted_at for r in rows)
    if timezone.is_naive(issued):
        issued = timezone.make_aware(issued, timezone.get_current_timezone())
    issued_tz = issued.astimezone(timezone.get_current_timezone())
    accepted_at_iso = issued_tz.isoformat()
    ips = sorted({(r.accepted_ip or "") for r in rows if r.accepted_ip})
    ips_joined = ",".join(ips)

    decl_row = UserAgreementAcceptanceDeclaration.objects.filter(
        user_id=user_id,
        acceptance_batch_id=acceptance_batch_id,
    ).first()
    declaration = (decl_row.declaration_text if decl_row else "") or ""

    msg_v2 = canonical_signing_message_v2(
        user_id=user_id,
        acceptance_batch_id=str(acceptance_batch_id),
        documents=docs,
        accepted_at_iso=accepted_at_iso,
        accepted_ips_joined=ips_joined,
        declaration=declaration,
    )
    expected = (signature_hex or "").strip().lower()
    if hmac.compare_digest(compute_hmac_hex(msg_v2), expected):
        return True

    msg_v1 = canonical_signing_message_legacy_v1(
        user_id=user_id,
        acceptance_batch_id=str(acceptance_batch_id),
        documents=docs,
        accepted_at_iso=accepted_at_iso,
        accepted_ips_joined=ips_joined,
    )
    return hmac.compare_digest(compute_hmac_hex(msg_v1), expected)


def latest_acceptance_batch_id(user: User) -> Optional[UUID]:
    row = (
        UserAgreementAcceptance.objects.filter(user_id=user.id)
        .values("acceptance_batch_id")
        .annotate(last_at=Max("accepted_at"))
        .order_by("-last_at")
        .values_list("acceptance_batch_id", flat=True)
        .first()
    )
    return row


def ensure_proof_for_batch(user: User, batch_id: UUID) -> Optional[UserAgreementAcceptanceProof]:
    """
    Return existing proof with PDF, or generate and persist one for this user+batch.
    Caller must ensure acceptances exist for the batch.
    """
    rows = list(
        UserAgreementAcceptance.objects.filter(user_id=user.id, acceptance_batch_id=batch_id)
        .select_related("document")
        .order_by("document_id")
    )
    if not rows:
        return None

    with transaction.atomic():
        proof = (
            UserAgreementAcceptanceProof.objects.select_for_update()
            .filter(user_id=user.id, acceptance_batch_id=batch_id)
            .first()
        )
        if proof and (getattr(proof.pdf_file, "name", None) or "").strip():
            return proof

        issued = min(r.accepted_at for r in rows)
        if timezone.is_naive(issued):
            issued = timezone.make_aware(issued, timezone.get_current_timezone())
        issued_tz = issued.astimezone(timezone.get_current_timezone())
        accepted_at_iso = issued_tz.isoformat()
        ips = sorted({(r.accepted_ip or "") for r in rows if r.accepted_ip})
        ips_joined = ",".join(ips)

        docs_payload = [
            {"id": r.document_id, "name": r.document.name, "version": r.version_accepted} for r in rows
        ]
        decl_row = UserAgreementAcceptanceDeclaration.objects.filter(
            user_id=user.id,
            acceptance_batch_id=batch_id,
        ).first()
        declaration = (decl_row.declaration_text if decl_row else "") or ""

        msg = canonical_signing_message_v2(
            user_id=user.id,
            acceptance_batch_id=str(batch_id),
            documents=docs_payload,
            accepted_at_iso=accepted_at_iso,
            accepted_ips_joined=ips_joined,
            declaration=declaration,
        )
        sig = compute_hmac_hex(msg)

        doc_rows = [(r.document.name, str(r.document_id), r.version_accepted) for r in rows]
        issued_display = issued_tz.strftime("%Y-%m-%d %H:%M:%S %Z")
        sig_dt_display = _format_signature_datetime(issued_tz)
        full_name = (getattr(user, "full_name", None) or "").strip()
        signed_by = full_name if full_name else f"Member (user id {user.id})"
        loc_line = ""
        addr = (getattr(settings, "COMPANY_ADDRESS", "") or "").strip()
        if addr:
            loc_line = addr.split("\n")[0].strip()[:200]
        elif (getattr(settings, "COMPANY_NAME", "") or "").strip():
            loc_line = settings.COMPANY_NAME.strip()
        else:
            loc_line = "India"

        pdf_bytes = build_acceptance_proof_pdf_bytes(
            user_id=user.id,
            user_display_name=full_name,
            acceptance_batch_id=str(batch_id),
            document_rows=doc_rows,
            issued_at_display=issued_display,
            issued_at_signature_display=sig_dt_display,
            accepted_ips=ips_joined,
            signature_hex=sig,
            declaration_text=declaration,
            signed_by_display=signed_by,
            location_display=loc_line,
        )
        safe_batch = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(batch_id))[:48]
        filename = f"acceptance_proof_{user.id}_{safe_batch}.pdf"

        if proof is None:
            proof = UserAgreementAcceptanceProof.objects.create(
                user=user,
                acceptance_batch_id=batch_id,
                signature_hex=sig,
                issued_at=issued_tz,
            )
        else:
            proof.signature_hex = sig
            proof.issued_at = issued_tz
            if (getattr(proof.pdf_file, "name", None) or "").strip():
                proof.pdf_file.delete(save=False)
            proof.save(update_fields=["signature_hex", "issued_at"])

        proof.pdf_file.save(filename, ContentFile(pdf_bytes), save=True)

    proof.refresh_from_db()
    return proof
