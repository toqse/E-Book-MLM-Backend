"""Admin Finance date parsing and aggregate endpoints."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.commissions.models import CommissionLedger
from apps.finance.services.date_range import parse_finance_range
from apps.payments.models import Order
from apps.users.models import User
from apps.users.services import allocate_member_identity


def _finance_admin() -> User:
    mid, ref, link = allocate_member_identity()
    return User.objects.create_user(
        login_identifier="fin-api@test.dev",
        password="pw",
        email="fin-api@test.dev",
        full_name="Finance API",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        role=User.Role.FINANCE,
        is_staff=True,
    )


def _member(phone: str) -> User:
    mid, ref, link = allocate_member_identity()
    u = User(phone=phone, full_name="M", member_id=mid, referral_code=ref, referral_link=link)
    u.set_unusable_password()
    u.save()
    return u


def _paid_order(user: User, order_number: str, **kwargs) -> Order:
    now = timezone.now()
    defaults = dict(
        user=user,
        order_number=order_number,
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("241.72"),
        is_retail_purchase=False,
        status=Order.Status.PAID,
        paid_at=now,
    )
    defaults.update(kwargs)
    return Order.objects.create(**defaults)


def test_parse_finance_range_swaps_inverted_bounds():
    r = parse_finance_range({"from": "2026-01-10", "to": "2026-01-01"})
    assert r.date_from == date(2026, 1, 1)
    assert r.date_to == date(2026, 1, 10)
    assert r.previous_date_to == date(2025, 12, 31)


def test_parse_finance_range_fy_label():
    r = parse_finance_range({"fy": "2025-26"})
    assert r.date_from == date(2025, 4, 1)
    assert r.date_to == date(2026, 3, 31)


def test_parse_finance_range_previous_window_length():
    r = parse_finance_range({"from": "2026-05-01", "to": "2026-05-03"})
    assert (r.date_to - r.date_from).days == 2
    assert (r.previous_date_to - r.previous_date_from).days == 2


@pytest.mark.django_db
def test_finance_overview_and_income_streams_and_commission_dates(system_config):
    admin = _finance_admin()
    buyer = _member("+918080099001")
    order = _paid_order(buyer, "ORD-FIN-1", razorpay_order_id="ord_fin_1")
    earner = _member("+918080099002")
    CommissionLedger.objects.create(
        recipient=earner,
        source_user=buyer,
        order=order,
        commission_type=CommissionLedger.CommissionType.DIRECT,
        amount=Decimal("30"),
        tds_deducted=Decimal("0"),
        net_amount=Decimal("30"),
        status=CommissionLedger.Status.CREDITED,
    )

    client = APIClient()
    client.force_authenticate(user=admin)
    d = timezone.localdate()
    params = {"from": d.isoformat(), "to": d.isoformat()}

    r = client.get("/api/v1/admin/finance/overview/", params)
    assert r.status_code == 200
    data = r.json()["data"]
    assert "kpis" in data
    assert data["kpis"]["gross_revenue"]["amount_paid"] == "241.72"

    inc = client.get("/api/v1/admin/finance/income-streams/", params).json()["data"]
    assert inc["total_income"] == "241.72"
    assert any(row["category_key"] == "mlm_standard" for row in inc["rows"])

    r2 = client.get("/api/v1/admin/commissions/", {**params, "exclude_milestone": "false"})
    assert r2.json()["data"]["count"] == 1

    r3 = client.get(
        "/api/v1/admin/commissions/",
        {"from": (d - timedelta(days=30)).isoformat(), "to": (d - timedelta(days=1)).isoformat()},
    )
    assert r3.json()["data"]["count"] == 0


@pytest.mark.django_db
def test_finance_export_csv_overview(system_config):
    admin = _finance_admin()
    client = APIClient()
    client.force_authenticate(user=admin)
    d = timezone.localdate()
    resp = client.post(
        "/api/v1/admin/finance/export/",
        {"scope": "overview", "format": "csv", "from": d.isoformat(), "to": d.isoformat()},
        format="json",
    )
    assert resp.status_code == 200
    cd = resp.get("Content-Disposition") or ""
    assert "overview" in cd.lower() or "finance" in cd.lower()
