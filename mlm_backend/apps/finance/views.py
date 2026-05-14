from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from rest_framework.request import Request

from apps.audit.services import write_audit
from apps.common.permissions import IsFinanceAdmin
from apps.common.responses import envelope_response

from .services.aggregates import (
    build_audit_trail,
    build_expenditure,
    build_income_streams,
    build_overview,
    build_tab_counts,
    build_tds_detail,
    finance_search,
    orders_finance_page,
)
from .services.date_range import parse_finance_range
from .services.export_service import run_finance_export, run_finance_report_pdf


def _parse_page_params(request: Request) -> tuple[int, int]:
    try:
        page = max(1, int(request.query_params.get("page", 1) or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(request.query_params.get("page_size", 20) or 20)
    except (TypeError, ValueError):
        page_size = 20
    page_size = max(1, min(page_size, 100))
    return page, page_size


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def finance_overview(request: Request):
    fr = parse_finance_range(request.query_params)
    return envelope_response(build_overview(fr))


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def finance_income_streams(request: Request):
    fr = parse_finance_range(request.query_params)
    return envelope_response(build_income_streams(fr))


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def finance_tab_counts(request: Request):
    fr = parse_finance_range(request.query_params)
    return envelope_response(build_tab_counts(fr))


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def finance_tds(request: Request):
    fr = parse_finance_range(request.query_params)
    return envelope_response(build_tds_detail(fr))


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def finance_expenditure(request: Request):
    fr = parse_finance_range(request.query_params)
    return envelope_response(build_expenditure(fr))


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def finance_audit_trail(request: Request):
    fr = parse_finance_range(request.query_params)
    page, page_size = _parse_page_params(request)
    q = (request.query_params.get("q") or "").strip()
    finance_only = str(request.query_params.get("finance_actions_only", "")).lower() in (
        "1",
        "true",
        "yes",
    )
    return envelope_response(
        build_audit_trail(fr, page=page, page_size=page_size, q=q, finance_actions_only=finance_only)
    )


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def finance_search_view(request: Request):
    fr = parse_finance_range(request.query_params)
    q = (request.query_params.get("q") or "").strip()
    try:
        limit = int(request.query_params.get("limit", 8) or 8)
    except (TypeError, ValueError):
        limit = 8
    return envelope_response(finance_search(fr, q, limit=limit))


@api_view(["GET"])
@permission_classes([IsFinanceAdmin])
def finance_orders(request: Request):
    fr = parse_finance_range(request.query_params)
    page, page_size = _parse_page_params(request)
    q = (request.query_params.get("q") or "").strip()
    return envelope_response(
        orders_finance_page(
            d0=fr.date_from,
            d1=fr.date_to,
            q=q,
            page=page,
            page_size=page_size,
        )
    )


@api_view(["POST"])
@permission_classes([IsFinanceAdmin])
def finance_export(request: Request):
    fr = parse_finance_range(request.query_params, body=request.data)
    scope = (request.data.get("scope") or request.query_params.get("scope") or "overview").strip().lower()
    fmt = (request.data.get("format") or request.data.get("export_format") or "csv").strip().lower()
    try:
        resp = run_finance_export(
            fr=fr,
            scope=scope,
            fmt=fmt,
            actor=request.user,
            ip_address=request.META.get("REMOTE_ADDR"),
        )
    except ValueError as e:
        code = str(e)
        if code == "invalid_format":
            msg = "Invalid format. Use csv or zip."
        elif code == "invalid_scope":
            msg = "Invalid scope for CSV export."
        elif code == "zip_requires_scope_all":
            msg = "ZIP export requires scope=all in the request body."
        else:
            msg = "Invalid export request."
        return envelope_response(None, message=msg, success=False, errors={"detail": code}, status=400)
    return resp


@api_view(["POST"])
@permission_classes([IsFinanceAdmin])
def finance_generate_report(request: Request):
    fr = parse_finance_range(request.query_params, body=request.data)
    write_audit(
        "finance.report_generated",
        actor=request.user,
        payload={"from": fr.date_from.isoformat(), "to": fr.date_to.isoformat()},
        ip_address=request.META.get("REMOTE_ADDR"),
    )
    return run_finance_report_pdf(fr=fr)
