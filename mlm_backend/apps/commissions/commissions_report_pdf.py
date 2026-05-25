"""ReportLab PDF for commission / earnings ledger exports (tabular)."""

from __future__ import annotations

from io import BytesIO
from typing import Iterable

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

# JUST200 brand palette — kept in sync with the invoice PDF so all
# customer-facing exports share the same orange identity.
ACCENT = colors.HexColor("#E5572B")
GREY_ROW = colors.HexColor("#f0f0f0")


def build_commissions_report_pdf_bytes(
    *,
    title: str,
    headers: list[str],
    rows: Iterable[list[str]],
    col_ratios: list[float] | None = None,
) -> bytes:
    """
    Render a tabular ledger PDF (landscape A4).

    `col_ratios` lets the caller hint relative column widths so wide cells
    (description, etc.) get more room and narrow ones (level, amounts) take
    less. If omitted, columns share the page width equally. Body cells are
    wrapped in ``Paragraph`` so long text flows onto multiple lines instead
    of overflowing into the next column.
    """
    buffer = BytesIO()
    page = landscape(A4)
    doc = SimpleDocTemplate(
        buffer,
        pagesize=page,
        rightMargin=12 * mm,
        leftMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title=title,
    )
    styles = getSampleStyleSheet()
    cell_style = ParagraphStyle(
        "LedgerCell",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=7,
        leading=8.5,
        alignment=TA_LEFT,
        wordWrap="CJK",
        spaceBefore=0,
        spaceAfter=0,
    )

    n_cols = max(len(headers), 1)
    tw = float(page[0]) - 24 * mm
    if col_ratios and len(col_ratios) == len(headers):
        total = sum(col_ratios) or float(len(col_ratios))
        col_widths = [tw * (r / total) for r in col_ratios]
    else:
        col_widths = [tw / n_cols] * n_cols

    def _wrap_row(row: list[str]) -> list[Paragraph]:
        return [Paragraph(str(c if c is not None else ""), cell_style) for c in row]

    data: list[list] = [list(headers)]
    for row in rows:
        data.append(_wrap_row(row))

    tbl = Table(data, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 8),
                ("FONTSIZE", (0, 1), (-1, -1), 7),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, GREY_ROW]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    title_style = ParagraphStyle(
        "LedgerTitle",
        parent=styles["Heading2"],
        textColor=ACCENT,
    )
    story = [
        Paragraph(title, title_style),
        Spacer(1, 6 * mm),
        tbl,
    ]
    doc.build(story)
    return buffer.getvalue()
