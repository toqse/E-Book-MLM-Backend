"""Agreement acceptance proof PDF.

Leading pages embed each accepted agreement's uploaded PDF as-is (preferred),
or a plain-text rendering of its ``content_html`` when no PDF file is attached.
The final page(s) hold the verification record (logo, meta, document table,
HMAC, declaration, digitally-signed block).
"""

from __future__ import annotations

import html as html_stdlib
import logging
import os
import re
from io import BytesIO
from xml.sax.saxutils import escape

from django.conf import settings
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

_logger = logging.getLogger(__name__)

ACCENT = colors.HexColor("#E5572B")

_PAGE_W = float(A4[0])
_MARGIN_X = 12 * mm
_CONTENT_W = _PAGE_W - 2 * _MARGIN_X

_LOGO_BOX_MAX_W = 20 * mm
_LOGO_BOX_MAX_H = 9 * mm

DIGITAL_SIGN_REASON = "OTP verified agreement acceptance"


def _html_to_plain_text(raw: str) -> str:
    """Strip admin-authored HTML to plain text for safe Paragraph rendering."""
    s = (raw or "").strip()
    if not s:
        return ""
    s = re.sub(r"(?is)<script[^>]*>.*?</script>", "", s)
    s = re.sub(r"(?is)<style[^>]*>.*?</style>", "", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</\s*(p|div|h[1-6]|section|article|header|footer|blockquote)\s*>", "\n\n", s)
    s = re.sub(r"(?i)</\s*li\s*>", "\n", s)
    s = re.sub(r"(?i)<\s*li[^>]*>", "\n• ", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html_stdlib.unescape(s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _signature_location_line() -> str:
    addr = (getattr(settings, "COMPANY_ADDRESS", "") or "").strip()
    if addr:
        return addr.split("\n")[0].strip()[:200]
    name = (getattr(settings, "COMPANY_NAME", "") or "").strip()
    if name:
        return name
    return "India"


def _logo_flowable():
    logo_path = os.path.join(str(settings.BASE_DIR), "just200_logo.png")
    if not os.path.exists(logo_path):
        return None
    logo = Image(logo_path)
    iw = float(logo.imageWidth) or 1.0
    ih = float(logo.imageHeight) or 1.0
    max_w, max_h = _LOGO_BOX_MAX_W, _LOGO_BOX_MAX_H
    dw = max_w
    dh = dw * (ih / iw)
    if dh > max_h:
        dh = max_h
        dw = dh * (iw / ih)
    logo.drawWidth = dw
    logo.drawHeight = dh
    logo.hAlign = "CENTER"
    return logo


def _new_doc(buffer: BytesIO) -> SimpleDocTemplate:
    margin_y = 10 * mm
    return SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=_MARGIN_X,
        leftMargin=_MARGIN_X,
        topMargin=margin_y,
        bottomMargin=margin_y,
        title="Agreement acceptance proof",
    )


def _build_cover_page_pdf_bytes() -> bytes:
    """Single-page cover with the logo, title, and IT Act subtitle."""
    buffer = BytesIO()
    doc = _new_doc(buffer)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "CoverTitle",
        parent=styles["Heading1"],
        fontSize=16,
        textColor=ACCENT,
        spaceAfter=6,
        leading=20,
        alignment=TA_CENTER,
    )
    subtitle_style = ParagraphStyle(
        "CoverSub",
        parent=styles["Normal"],
        fontSize=9,
        leading=12,
        alignment=TA_CENTER,
    )

    story: list = []
    logo = _logo_flowable()
    if logo:
        story.append(Table([[logo]], colWidths=[_CONTENT_W]))
        story[-1].setStyle(
            TableStyle(
                [
                    ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
                ]
            )
        )
        story.append(Spacer(1, 2 * mm))
    story.append(Paragraph("Agreement acceptance — verification record", title_style))
    story.append(
        Paragraph(
            "Record under Information Technology Act, 2000 (electronic records and authentication). "
            "Integrity token is HMAC-SHA256 over canonical JSON stored server-side.",
            subtitle_style,
        )
    )
    doc.build(story)
    out = buffer.getvalue()
    buffer.close()
    return out


def _build_html_appendix_pdf_bytes(documents: list[dict]) -> bytes:
    """Render plain-text fallback pages for documents that have no uploaded PDF."""
    buffer = BytesIO()
    doc = _new_doc(buffer)
    styles = getSampleStyleSheet()
    heading_style = ParagraphStyle(
        "AppendixH",
        parent=styles["Heading1"],
        fontSize=12,
        textColor=ACCENT,
        spaceAfter=6,
        leading=15,
        alignment=TA_CENTER,
    )
    doc_title = ParagraphStyle(
        "AppendixDoc",
        parent=styles["Heading2"],
        fontSize=10,
        textColor=ACCENT,
        spaceAfter=4,
        spaceBefore=2,
        leading=13,
    )
    body = ParagraphStyle(
        "AppendixBody",
        parent=styles["Normal"],
        fontSize=8.5,
        leading=11,
    )

    story: list = [
        Paragraph("Accepted agreement text", heading_style),
        Spacer(1, 2 * mm),
    ]
    for idx, item in enumerate(documents):
        if idx:
            story.append(PageBreak())
        name = item.get("name", "")
        ver = item.get("version", "")
        story.append(
            Paragraph(
                f"{escape(name)} <font size='9' color='#555555'>(version {escape(ver)})</font>",
                doc_title,
            )
        )
        plain = _html_to_plain_text(item.get("content_html") or "")
        blocks = [b.strip() for b in re.split(r"\n\n+", plain) if b.strip()] if plain else []
        if not blocks:
            story.append(
                Paragraph(
                    "No embedded agreement text on file for this version; refer to the official "
                    "document PDF or the member portal.",
                    body,
                )
            )
        else:
            for block in blocks:
                story.append(Paragraph(escape(block).replace("\n", "<br/>"), body))
                story.append(Spacer(1, 2 * mm))
        story.append(Spacer(1, 3 * mm))

    doc.build(story)
    out = buffer.getvalue()
    buffer.close()
    return out


def _build_verification_record_pdf_bytes(
    *,
    user_display_name: str,
    acceptance_batch_id: str,
    document_rows: list[tuple[str, str, str]],
    issued_at_display: str,
    issued_at_signature_display: str,
    accepted_ips: str,
    signature_hex: str,
    declaration_text: str,
    signed_by_display: str,
    location_display: str,
) -> bytes:
    buffer = BytesIO()
    doc = _new_doc(buffer)
    styles = getSampleStyleSheet()
    body = ParagraphStyle("ProofBody", parent=styles["Normal"], fontSize=9, leading=12)
    small = ParagraphStyle("Small", parent=styles["Normal"], fontSize=8, leading=11)
    sig_label = ParagraphStyle("SigLbl", parent=small, fontSize=8, leading=11)

    story: list = []

    meta_html = (
        f"<b>Name:</b> {escape(user_display_name or '')}<br/>"
        f"<b>Acceptance batch id:</b> {escape(acceptance_batch_id)}<br/>"
        f"<b>Issued at (server):</b> {escape(issued_at_display)}<br/>"
        f"<b>Client IP(s) recorded:</b> {escape(accepted_ips or '—')}"
    )
    story.append(Paragraph(meta_html, small))
    story.append(Spacer(1, 3 * mm))

    tbl_data = [["Document", "Id", "Version accepted"]]
    for name, did, ver in document_rows:
        tbl_data.append([escape(name), escape(did), escape(ver)])

    doc_col = _CONTENT_W * 0.58
    id_col = _CONTENT_W * 0.14
    ver_col = _CONTENT_W - doc_col - id_col
    t = Table(tbl_data, colWidths=[doc_col, id_col, ver_col])
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 8.5),
                ("FONTSIZE", (0, 1), (-1, -1), 8.5),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(t)
    story.append(Spacer(1, 3 * mm))

    sig_style = ParagraphStyle("Sig", parent=body, fontName="Courier", fontSize=7, leading=9)
    story.append(Paragraph("<b>HMAC-SHA256 (hex)</b>", small))
    story.append(Paragraph(escape(signature_hex), sig_style))
    story.append(Spacer(1, 4 * mm))

    decl_heading = ParagraphStyle(
        "DeclH", parent=styles["Heading2"], fontSize=10, textColor=ACCENT, spaceAfter=2
    )
    story.append(Paragraph("Declaration", decl_heading))
    decl_body = (declaration_text or "").strip() or "—"
    decl_style = ParagraphStyle(
        "DeclBody",
        parent=styles["Normal"],
        fontSize=8.5,
        leading=11,
    )
    story.append(Paragraph(escape(decl_body).replace("\n", "<br/>"), decl_style))
    story.append(Spacer(1, 4 * mm))

    signed_line = escape(signed_by_display or "Member")
    loc_line = escape(location_display or _signature_location_line())

    left_txt = (
        f"<b>Digitally signed by</b> {signed_line}<br/>"
        f"<b>Date:</b> {escape(issued_at_signature_display)}<br/>"
        f"<b>Reason:</b> {escape(DIGITAL_SIGN_REASON)}<br/>"
        f"<b>Location:</b> {loc_line}"
    )
    left_cell = Paragraph(left_txt, sig_label)
    check_para = Paragraph(
        "<font size='25' color='#22aa44'><b>✓</b></font>",
        ParagraphStyle("Chk", parent=styles["Normal"], alignment=TA_RIGHT, fontSize=22),
    )
    check_col_w = 26 * mm
    sig_w = _CONTENT_W - check_col_w
    sig_tbl = Table([[left_cell, check_para]], colWidths=[sig_w, check_col_w])
    sig_tbl.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fafafa")),
            ]
        )
    )
    story.append(sig_tbl)

    doc.build(story)
    out = buffer.getvalue()
    buffer.close()
    return out


def _append_pdf_bytes(writer: PdfWriter, src_pdf: bytes) -> bool:
    """Append every page from ``src_pdf`` into ``writer``. Returns True on success."""
    try:
        reader = PdfReader(BytesIO(src_pdf))
        for page in reader.pages:
            writer.add_page(page)
        return True
    except Exception as exc:  # pragma: no cover — defensive: corrupt PDFs should not break the proof
        _logger.warning("Failed to merge agreement PDF into acceptance proof: %s", exc)
        return False


def build_acceptance_proof_pdf_bytes(
    *,
    user_id: int,
    user_display_name: str,
    acceptance_batch_id: str,
    document_rows: list[tuple[str, str, str]],
    issued_at_display: str,
    issued_at_signature_display: str,
    accepted_ips: str,
    signature_hex: str,
    declaration_text: str,
    signed_by_display: str,
    location_display: str,
    agreement_documents: list[dict] | None = None,
    agreement_appendix: list[tuple[str, str, str]] | None = None,
) -> bytes:
    """
    Build a merged acceptance proof PDF:
      1. Each accepted document's uploaded PDF (``pdf_bytes``) as-is.
      2. Fallback HTML-text appendix for documents that have no uploaded PDF.
      3. Verification record (logo, meta, document table, HMAC, declaration,
         digitally-signed block) as the final page(s).

    ``agreement_documents`` (preferred): list of dicts, each with keys
      ``name``, ``version``, ``pdf_bytes`` (bytes | None), ``content_html`` (str).
    ``agreement_appendix`` (legacy): list of (name, version, content_html);
      treated as documents with no uploaded PDF.
    """
    if agreement_documents is None and agreement_appendix:
        agreement_documents = [
            {"name": name, "version": ver, "pdf_bytes": None, "content_html": html}
            for name, ver, html in agreement_appendix
        ]
    agreement_documents = agreement_documents or []

    verification_pdf = _build_verification_record_pdf_bytes(
        user_display_name=user_display_name,
        acceptance_batch_id=acceptance_batch_id,
        document_rows=document_rows,
        issued_at_display=issued_at_display,
        issued_at_signature_display=issued_at_signature_display,
        accepted_ips=accepted_ips,
        signature_hex=signature_hex,
        declaration_text=declaration_text,
        signed_by_display=signed_by_display,
        location_display=location_display,
    )

    if not agreement_documents:
        return verification_pdf

    writer = PdfWriter()

    cover_pdf = _build_cover_page_pdf_bytes()
    _append_pdf_bytes(writer, cover_pdf)

    html_fallback_docs: list[dict] = []
    for item in agreement_documents:
        pdf_bytes = item.get("pdf_bytes")
        if pdf_bytes and _append_pdf_bytes(writer, pdf_bytes):
            continue
        if (item.get("content_html") or "").strip():
            html_fallback_docs.append(item)

    if html_fallback_docs:
        fallback_pdf = _build_html_appendix_pdf_bytes(html_fallback_docs)
        _append_pdf_bytes(writer, fallback_pdf)

    if not _append_pdf_bytes(writer, verification_pdf):
        return verification_pdf

    out_buf = BytesIO()
    writer.write(out_buf)
    out = out_buf.getvalue()
    out_buf.close()
    return out


def acceptance_proof_pdf_page_count(pdf_bytes: bytes) -> int:
    """Count page objects (excludes /Type /Pages parent). Best-effort for ReportLab output."""
    return len(re.findall(rb"/Type\s*/Page(?!\w)", pdf_bytes))
