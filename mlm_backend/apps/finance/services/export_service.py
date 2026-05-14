"""CSV/ZIP exports for Admin Finance."""

from __future__ import annotations

import csv
import io
import zipfile
from typing import Any

from django.http import HttpResponse

from apps.audit.services import write_audit
from apps.commissions.admin_ledger_services import (
    apply_admin_commission_filters,
    base_ledger_queryset,
    parse_admin_commission_filters,
)
from apps.commissions.user_views import _EXPORT_HEADERS, _export_row_cells

from .aggregates import (
    build_expenditure,
    build_income_streams,
    build_overview,
    build_tds_detail,
)
from .date_range import FinanceDateRange


def _csv_response(filename: str, rows: list[list[Any]], header: list[str] | None = None) -> HttpResponse:
    buf = io.StringIO()
    w = csv.writer(buf)
    if header:
        w.writerow(header)
    for row in rows:
        w.writerow(row)
    resp = HttpResponse(buf.getvalue(), content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


def _overview_csv(fr: FinanceDateRange) -> HttpResponse:
    ov = build_overview(fr)
    rows: list[list[Any]] = []
    r = ov.get("range", {})
    for k in ("from", "to", "preset", "previous_from", "previous_to"):
        if k in r:
            rows.append([f"range.{k}", r[k]])
    kpis = ov.get("kpis", {})
    for section, payload in kpis.items():
        if isinstance(payload, dict):
            for k2, v2 in payload.items():
                if isinstance(v2, dict):
                    for k3, v3 in v2.items():
                        rows.append([f"kpis.{section}.{k2}.{k3}", str(v3)])
                else:
                    rows.append([f"kpis.{section}.{k2}", str(v2)])
        else:
            rows.append([f"kpis.{section}", str(payload)])
    return _csv_response("finance_overview.csv", rows, ["key", "value"])


def _income_streams_csv(fr: FinanceDateRange) -> HttpResponse:
    data = build_income_streams(fr)
    rows = [
        [r["category_key"], r["label"], r["amount"], r["share_percent"], r["trend_percent"] or ""]
        for r in data["rows"]
    ]
    return _csv_response(
        "finance_income_streams.csv",
        rows,
        ["category_key", "label", "amount", "share_percent", "trend_percent"],
    )


def _commissions_csv(fr: FinanceDateRange) -> HttpResponse:
    qdict = {
        "from": fr.date_from.isoformat(),
        "to": fr.date_to.isoformat(),
        "exclude_milestone": "0",
    }
    flt = parse_admin_commission_filters(qdict)
    qs = apply_admin_commission_filters(base_ledger_queryset(), flt).order_by("-id")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_EXPORT_HEADERS)
    for row in qs.iterator():
        w.writerow(_export_row_cells(row))
    resp = HttpResponse(buf.getvalue(), content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="commissions.csv"'
    return resp


def _withdrawals_csv(fr: FinanceDateRange) -> HttpResponse:
    from apps.wallet.models import WithdrawalRequest

    qs = WithdrawalRequest.objects.select_related("user").filter(
        created_at__date__gte=fr.date_from,
        created_at__date__lte=fr.date_to,
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "id",
            "member_id",
            "full_name",
            "status",
            "amount_requested",
            "tds_amount",
            "net_payable",
            "payout_method",
            "created_at",
            "paid_at",
        ]
    )
    for x in qs.order_by("-id").iterator():
        w.writerow(
            [
                x.id,
                x.user.member_id,
                x.user.full_name,
                x.status,
                str(x.amount_requested),
                str(x.tds_amount),
                str(x.net_payable),
                x.payout_method,
                x.created_at.isoformat(),
                x.paid_at.isoformat() if x.paid_at else "",
            ]
        )
    resp = HttpResponse(buf.getvalue(), content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="withdrawals.csv"'
    return resp


def _tds_csv(fr: FinanceDateRange) -> HttpResponse:
    d = build_tds_detail(fr)
    rows: list[list[str]] = [
        ["commissions_tds_on_credited_rows", d["commissions_tds_on_credited_rows"]],
        ["milestones_tds_on_credited_rows", d["milestones_tds_on_credited_rows"]],
        ["withdrawals_tds_on_paid_payouts", d["withdrawals_tds_on_paid_payouts"]],
        ["total_tds", d["total_tds"]],
    ]
    for sec in d.get("withdrawals_by_section", []):
        rows.append([f"withdrawal_section:{sec['section']}", sec["amount"]])
    return _csv_response("finance_tds.csv", rows, ["key", "value"])


def _expenditure_csv(fr: FinanceDateRange) -> HttpResponse:
    ex = build_expenditure(fr)
    rows = [[r["category_key"], r["label"], r["amount"]] for r in ex["rows"]]
    rows.append(["total", "", ex["total"]])
    return _csv_response("finance_expenditure.csv", rows, ["category_key", "label", "amount"])


def run_finance_export(
    *,
    fr: FinanceDateRange,
    scope: str,
    fmt: str,
    actor,
    ip_address: str | None,
) -> HttpResponse:
    scope = (scope or "overview").strip().lower()
    fmt = (fmt or "csv").strip().lower()
    if fmt not in ("csv", "zip"):
        raise ValueError("invalid_format")
    if fmt == "zip" and scope != "all":
        raise ValueError("zip_requires_scope_all")

    if fmt == "csv":
        if scope == "overview":
            resp = _overview_csv(fr)
        elif scope == "income":
            resp = _income_streams_csv(fr)
        elif scope == "commissions":
            resp = _commissions_csv(fr)
        elif scope == "withdrawals":
            resp = _withdrawals_csv(fr)
        elif scope == "tds":
            resp = _tds_csv(fr)
        elif scope == "expenditure":
            resp = _expenditure_csv(fr)
        else:
            raise ValueError("invalid_scope")
        write_audit(
            "finance.export",
            actor=actor,
            payload={"scope": scope, "format": fmt, "from": fr.date_from.isoformat(), "to": fr.date_to.isoformat()},
            ip_address=ip_address,
        )
        return resp

    # zip all
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, gen in [
            ("overview.csv", lambda: _overview_csv(fr).content),
            ("income_streams.csv", lambda: _income_streams_csv(fr).content),
            ("commissions.csv", lambda: _commissions_csv(fr).content),
            ("withdrawals.csv", lambda: _withdrawals_csv(fr).content),
            ("tds.csv", lambda: _tds_csv(fr).content),
            ("expenditure.csv", lambda: _expenditure_csv(fr).content),
        ]:
            zf.writestr(name, gen().decode("utf-8"))
    mem.seek(0)
    resp = HttpResponse(mem.getvalue(), content_type="application/zip")
    resp["Content-Disposition"] = 'attachment; filename="finance_export.zip"'
    write_audit(
        "finance.export",
        actor=actor,
        payload={"scope": "all", "format": "zip", "from": fr.date_from.isoformat(), "to": fr.date_to.isoformat()},
        ip_address=ip_address,
    )
    return resp


def run_finance_report_pdf(*, fr: FinanceDateRange) -> HttpResponse:
    from apps.commissions.commissions_report_pdf import build_commissions_report_pdf_bytes

    ov = build_overview(fr)
    kpis = ov.get("kpis", {})
    rows: list[list[str]] = []
    for section, payload in kpis.items():
        if not isinstance(payload, dict):
            rows.append([section, str(payload)])
            continue
        for k, v in payload.items():
            if isinstance(v, dict):
                for k2, v2 in v.items():
                    rows.append([f"{section}.{k}.{k2}", str(v2)])
            else:
                rows.append([f"{section}.{k}", str(v)])
    pdf_bytes = build_commissions_report_pdf_bytes(
        title=f"Finance report {fr.date_from} .. {fr.date_to}",
        headers=["metric", "value"],
        rows=rows,
    )
    resp = HttpResponse(pdf_bytes, content_type="application/pdf")
    resp["Content-Disposition"] = 'attachment; filename="finance_report.pdf"'
    return resp
