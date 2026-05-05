"""GST invoice PDF (ReportLab). Layout inspired by standard invoice templates."""

from __future__ import annotations

import logging
import os
from io import BytesIO
from xml.sax.saxutils import escape

from django.conf import settings
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from apps.users.models import User

from .models import GSTInvoice, Order

logger = logging.getLogger(__name__)

ACCENT = colors.HexColor("#2c73a9")
GREY_ROW = colors.HexColor("#f0f0f0")


def _fmt_money(amount) -> str:
    # Use ASCII currency prefix to avoid missing-glyph squares on some PDF viewers/fonts.
    return f"Rs. {float(amount):,.2f}"


def _buyer_lines(order: Order, user: User) -> list[str]:
    lines = [user.full_name or "Customer"]
    if order.billing_line1:
        lines.append(order.billing_line1)
    if order.billing_line2:
        lines.append(order.billing_line2)
    city_parts = [p for p in (order.billing_city, order.billing_state, order.billing_postal_code) if p]
    if city_parts:
        lines.append(", ".join(city_parts))
    if order.billing_country:
        lines.append(order.billing_country)
    return lines


def _company_lines() -> list[str]:
    rows = []
    if getattr(settings, "COMPANY_NAME", None):
        rows.append(settings.COMPANY_NAME)
    addr = getattr(settings, "COMPANY_ADDRESS", "") or ""
    if addr.strip():
        for part in addr.split("\n"):
            p = part.strip()
            if p:
                rows.append(p)
    contact = []
    if getattr(settings, "COMPANY_PHONE", ""):
        contact.append(settings.COMPANY_PHONE)
    if getattr(settings, "COMPANY_EMAIL", ""):
        contact.append(settings.COMPANY_EMAIL)
    if getattr(settings, "COMPANY_WEBSITE", ""):
        contact.append(settings.COMPANY_WEBSITE)
    gstn = getattr(settings, "GST_NUMBER", "") or ""
    if gstn.strip():
        rows.append(f"GSTIN: {gstn.strip()}")
    if contact:
        rows.append(" | ".join(contact))
    return rows or [settings.COMPANY_NAME]


def _company_logo() -> Image | None:
    logo_path = os.path.join(str(settings.BASE_DIR), "just200_logo.png")
    if not os.path.exists(logo_path):
        return None
    logo = Image(logo_path)
    logo.drawWidth = 36 * mm
    logo.drawHeight = 36 * mm
    logo.hAlign = "LEFT"
    return logo


def build_invoice_pdf_bytes(order: Order, inv: GSTInvoice) -> bytes:
    user = order.user
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "InvTitle",
        parent=styles["Heading1"],
        fontSize=22,
        textColor=ACCENT,
        spaceAfter=6,
        leading=26,
    )
    label_style = ParagraphStyle(
        "Lbl",
        parent=styles["Normal"],
        fontSize=9,
        textColor=colors.HexColor("#333333"),
    )
    small = ParagraphStyle(
        "Sml",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.HexColor("#555555"),
    )
    right_small = ParagraphStyle(
        "RSml",
        parent=small,
        alignment=TA_RIGHT,
    )

    story: list = []

    def _header_footer(cv: canvas.Canvas, _doc):
        w, h = A4
        cv.saveState()
        cv.setFillColor(ACCENT)
        cv.rect(0, h - 4 * mm, w, 4 * mm, stroke=0, fill=1)
        cv.rect(0, 0, w, 4 * mm, stroke=0, fill=1)
        cv.restoreState()

    paid = order.paid_at or timezone.now()
    inv_date = timezone.localtime(paid).strftime("%d %B %Y").upper()

    clines = _company_lines()
    # Top row: logo + company name (left), company details (right)
    company_para = Paragraph(
        "<b>" + escape(clines[0] if clines else settings.COMPANY_NAME or "Company") + "</b>",
        ParagraphStyle("CompanyName", parent=label_style, fontSize=12),
    )
    left_flowables = []
    logo = _company_logo()
    if logo:
        left_flowables.append(logo)
        left_flowables.append(Spacer(1, 3 * mm))
    left_flowables.append(company_para)

    hdr_right_body = "<br/>".join(escape(x) for x in (clines[1:] or [])) or "&nbsp;"
    hdr_right = Paragraph(hdr_right_body, right_small)
    top_table = Table([[left_flowables, hdr_right]], colWidths=[88 * mm, 86 * mm])
    top_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("ALIGN", (0, 0), (0, 0), "LEFT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(top_table)
    story.append(Spacer(1, 8 * mm))
    story.append(Paragraph("INVOICE", title_style))
    story.append(Spacer(1, 6 * mm))

    due_cell = Paragraph(
        f"<b><font color='white' size='14'>{_fmt_money(order.amount_paid)}</font></b>",
        ParagraphStyle("due", alignment=TA_LEFT, fontSize=12, leading=14),
    )
    due_tbl = Table(
        [[due_cell]],
        colWidths=[55 * mm],
        rowHeights=[14 * mm],
    )
    due_tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), ACCENT),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )

    inv_no = escape(inv.invoice_number)
    left_meta = Paragraph(
        f"<b>INVOICE NO:</b> {inv_no}<br/>"
        f"<b>INVOICE DATE:</b> {inv_date}<br/>"
        f"<b>DUE TOTAL:</b><br/>",
        label_style,
    )
    left_block = Table([[left_meta], [due_tbl]], colWidths=[62 * mm])
    left_block.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )

    bill_lines = "<br/>".join(escape(x) for x in _buyer_lines(order, user))
    inv_to = Paragraph(f"<b>INVOICE TO:</b><br/>{bill_lines}", label_style)
    right_block = Table([[inv_to]], colWidths=[112 * mm])
    right_block.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.5, ACCENT),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
            ]
        )
    )

    hdr_row = Table([[left_block, right_block]], colWidths=[62 * mm, 112 * mm])
    hdr_row.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (0, 0), (0, 0), "LEFT"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(hdr_row)
    story.append(Spacer(1, 10 * mm))

    hsn = escape(str(inv.hsn_sac_code))
    lines = list(order.lines.select_related("ebook").order_by("id"))
    if lines:
        goods_data = [["Item Description", "Price", "Qty", "Total"]]
        for line in lines:
            ebook_title = escape(line.ebook.title)
            desc = Paragraph(
                f"<b>{ebook_title}</b><br/><font size='8' color='#666666'>HSN/SAC {hsn}</font>",
                label_style,
            )
            unit = _fmt_money(line.unit_base_price)
            goods_data.append([desc, unit, "1", unit])
    else:
        ebook_title = escape(order.ebook.title if order.ebook else "Digital product")
        desc = Paragraph(
            f"<b>{ebook_title}</b><br/><font size='8' color='#666666'>HSN/SAC {hsn}</font>",
            label_style,
        )
        qty = "1"
        unit = _fmt_money(order.base_price)
        line_total = _fmt_money(order.base_price)
        goods_data = [["Item Description", "Price", "Qty", "Total"], [desc, unit, qty, line_total]]

    tbl = Table(
        goods_data,
        colWidths=[80 * mm, 33 * mm, 21 * mm, 40 * mm],
    )
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 9),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, GREY_ROW]),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    story.append(tbl)
    story.append(Spacer(1, 12 * mm))

    pay_text = getattr(settings, "INVOICE_PAYMENT_DETAILS", "").strip()
    if not pay_text:
        pay_text = "Paid via Razorpay (online)."
    payment_para = Paragraph(
        "<br/>".join(escape(line) for line in pay_text.split("\n")),
        small,
    )
    summary_rows = [
        ["Sub Total (taxable)", _fmt_money(inv.base_amount)],
        ["CGST", _fmt_money(inv.cgst)],
        ["SGST", _fmt_money(inv.sgst)],
        ["Gateway / convenience", _fmt_money(order.gateway_charge)],
    ]
    if order.discount_amount and order.discount_amount > 0:
        summary_rows.append(["Discount", f"-{_fmt_money(order.discount_amount)}"])

    grand_para = Paragraph(
        f"<b><font color='white' size='12'>Grand Total: {_fmt_money(order.amount_paid)}</font></b>",
        ParagraphStyle("gt", alignment=TA_RIGHT),
    )
    grand_box = Table([[grand_para]], colWidths=[70 * mm], rowHeights=[12 * mm])
    grand_box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), ACCENT),
                ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )

    sum_table = Table(summary_rows, colWidths=[54 * mm, 33 * mm])
    sum_table.setStyle(
        TableStyle(
            [
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("LINEABOVE", (0, 0), (-1, 0), 0.25, colors.grey),
                ("LINEBELOW", (0, -1), (-1, -1), 0.25, colors.grey),
            ]
        )
    )

    pay_hdr = Paragraph("<b>Payment</b>", label_style)
    bottom = Table(
        [
            [pay_hdr, ""],
            [payment_para, sum_table],
            [Spacer(1, 4 * mm), Spacer(1, 4 * mm)],
            [Paragraph("<b>Terms &amp; conditions</b>", label_style), grand_box],
        ],
        colWidths=[87 * mm, 87 * mm],
    )
    bottom.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 1), (1, 1), "RIGHT"),
                ("ALIGN", (1, 3), (1, 3), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(bottom)

    terms = getattr(settings, "INVOICE_TERMS_AND_CONDITIONS", "").strip()
    if not terms:
        terms = (
            "1. This is a computer-generated tax invoice for your e-book purchase.\n"
            "2. For support, contact us using the company details above.\n"
            "3. GST is split as CGST/SGST (intra-state) as shown."
        )
    story.append(Spacer(1, 8 * mm))
    story.append(
        Paragraph("<br/>".join(escape(line) for line in terms.split("\n")), small)
    )
    story.append(Spacer(1, 10 * mm))
    story.append(
        Paragraph(
            "<i>Authorized signatory</i><br/>" + escape(settings.COMPANY_NAME or "Company"),
            small,
        )
    )

    doc.build(story, onFirstPage=_header_footer, onLaterPages=_header_footer)
    pdf = buffer.getvalue()
    buffer.close()
    if not pdf.startswith(b"%PDF"):
        logger.error("invoice_pdf_invalid_magic order_id=%s", order.pk)
    return pdf
