"""Agreement acceptance proof PDF (ReportLab): logo, attestation block, declaration, HMAC."""

from __future__ import annotations

import os
from io import BytesIO
from xml.sax.saxutils import escape

from django.conf import settings
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ACCENT = colors.HexColor("#2c73a9")

DIGITAL_SIGN_REASON = "OTP verified agreement acceptance"


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
    logo.drawWidth = 48 * mm
    ih, iw = float(logo.imageHeight), float(logo.imageWidth)
    logo.drawHeight = logo.drawWidth * (ih / iw) if iw else 48 * mm
    logo.hAlign = "CENTER"
    return logo


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
) -> bytes:
    """
    document_rows: list of (document_name, document_id_str, version_accepted)
    issued_at_signature_display: e.g. 2020.09.08 13:18:17 +0530
    """
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=14 * mm,
        bottomMargin=16 * mm,
        title="Agreement acceptance proof",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ProofTitle",
        parent=styles["Heading1"],
        fontSize=16,
        textColor=ACCENT,
        spaceAfter=8,
        alignment=TA_CENTER,
    )
    body = ParagraphStyle("ProofBody", parent=styles["Normal"], fontSize=10, leading=14)
    body_center = ParagraphStyle("ProofBodyC", parent=body, alignment=TA_CENTER)
    small = ParagraphStyle("Small", parent=styles["Normal"], fontSize=9, leading=12)
    sig_label = ParagraphStyle("SigLbl", parent=small, fontSize=9, leading=13)

    story: list = []

    logo = _logo_flowable()
    if logo:
        story.append(Table([[logo]], colWidths=[174 * mm]))
        story[-1].setStyle(
            TableStyle(
                [
                    ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        story.append(Spacer(1, 4 * mm))

    story.append(Paragraph("Agreement acceptance — verification record", title_style))
    story.append(
        Paragraph(
            "Record under Information Technology Act, 2000 (electronic records and authentication). "
            "Integrity token below is HMAC-SHA256 over canonical JSON stored server-side.",
            body_center,
        )
    )
    story.append(Spacer(1, 8 * mm))

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
        "<font size='28' color='#22aa44'><b>✓</b></font>",
        ParagraphStyle("Chk", parent=styles["Normal"], alignment=TA_RIGHT, fontSize=28),
    )
    sig_tbl = Table([[left_cell, check_para]], colWidths=[142 * mm, 28 * mm])
    sig_tbl.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("BOX", (0, 0), (-1, -1), 0.6, colors.grey),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fafafa")),
            ]
        )
    )
    story.append(sig_tbl)
    story.append(Spacer(1, 8 * mm))

    decl_heading = ParagraphStyle("DeclH", parent=styles["Heading2"], fontSize=12, textColor=ACCENT)
    story.append(Paragraph("Declaration", decl_heading))
    decl_body = (declaration_text or "").strip() or "—"
    story.append(Paragraph(escape(decl_body).replace("\n", "<br/>"), body))
    story.append(Spacer(1, 8 * mm))

    meta_html = (
        f"<b>Member user id:</b> {user_id}<br/>"
        f"<b>Name:</b> {escape(user_display_name or '')}<br/>"
        f"<b>Acceptance batch id:</b> {escape(acceptance_batch_id)}<br/>"
        f"<b>Issued at (server):</b> {escape(issued_at_display)}<br/>"
        f"<b>Client IP(s) recorded:</b> {escape(accepted_ips or '—')}"
    )
    story.append(Paragraph(meta_html, body))
    story.append(Spacer(1, 6 * mm))

    tbl_data = [["Document", "Id", "Version accepted"]]
    for name, did, ver in document_rows:
        tbl_data.append([escape(name), escape(did), escape(ver)])

    t = Table(tbl_data, colWidths=[95 * mm, 22 * mm, 45 * mm])
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 9),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(t)
    story.append(Spacer(1, 8 * mm))

    sig_style = ParagraphStyle("Sig", parent=body, fontName="Courier", fontSize=8, leading=10)
    story.append(Paragraph("<b>HMAC-SHA256 (hex)</b>", body))
    story.append(Paragraph(escape(signature_hex), sig_style))

    doc.build(story)
    out = buffer.getvalue()
    buffer.close()
    return out
