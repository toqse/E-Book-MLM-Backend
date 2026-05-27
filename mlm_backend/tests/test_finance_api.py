"""Admin Finance date parsing and aggregate endpoints."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.commissions.models import CommissionLedger
from apps.courses.models import EBook
from apps.finance.services.date_range import parse_finance_range
from apps.payments.models import CreditNote, GSTInvoice, Order, OrderLine, RefundRequest
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
    assert data["kpis"]["orders_count"]["total_paid"] == 1
    assert data["kpis"]["orders_count"]["actual_paid"] == 1
    assert data["kpis"]["orders_count"]["sponsor_slot"] == 0
    assert data["kpis"]["orders_count"]["single_book"] == 1
    assert data["kpis"]["orders_count"]["multi_book"] == 0
    assert data["kpis"]["gateway_charges"]["amount"] == "5.72"
    assert data["kpis"]["refunds_approved"]["amount"] == "0.00"
    for key in (
        "gross_revenue",
        "commission_paid_net",
        "payouts_processed_net",
        "net_platform_income",
        "tds_deducted",
        "milestone_bonuses",
        "sponsor_slots",
        "gst_collected",
        "orders_count",
        "refunds_approved",
        "gateway_charges",
    ):
        formula = data["kpis"][key].get("formula")
        assert isinstance(formula, list) and formula
        assert all(isinstance(s, str) and s for s in formula)
    assert any(
        "241.72" in line
        for line in data["kpis"]["gross_revenue"]["formula"]
    )
    assert any(
        "= 5.72" in line
        for line in data["kpis"]["gateway_charges"]["formula"]
    )

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
def test_finance_overview_orders_count_split(system_config):
    admin = _finance_admin()
    buyer = _member("+918080099010")
    now = timezone.now()
    d = timezone.localdate()
    params = {"from": d.isoformat(), "to": d.isoformat()}

    _paid_order(buyer, "ORD-FIN-SINGLE")

    eb1 = EBook.objects.create(
        title="Fin A",
        slug="fin-book-a",
        category="X",
        description="d",
        pages_count=1,
        language="English",
        price=200,
        status=EBook.Status.PUBLISHED,
        file_url="https://example.com/a.pdf",
        is_primary=False,
        is_active=True,
    )
    eb2 = EBook.objects.create(
        title="Fin B",
        slug="fin-book-b",
        category="X",
        description="d",
        pages_count=1,
        language="English",
        price=200,
        status=EBook.Status.PUBLISHED,
        file_url="https://example.com/b.pdf",
        is_primary=False,
        is_active=True,
    )
    multi = Order.objects.create(
        user=buyer,
        ebook=eb1,
        order_number="ORD-FIN-MULTI",
        base_price=Decimal("400"),
        gst_amount=Decimal("72"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("477.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("477.72"),
        is_retail_purchase=False,
        status=Order.Status.PAID,
        paid_at=now,
    )
    OrderLine.objects.create(order=multi, ebook=eb1, unit_base_price=Decimal("200"))
    OrderLine.objects.create(order=multi, ebook=eb2, unit_base_price=Decimal("200"))

    _paid_order(
        buyer,
        "ORD-FIN-SLOT",
        is_sponsor_slot_redemption=True,
        amount_paid=Decimal("0"),
        gateway_charge=Decimal("0"),
        base_price=Decimal("0"),
        gst_amount=Decimal("0"),
        total_amount=Decimal("0"),
    )

    client = APIClient()
    client.force_authenticate(user=admin)
    data = client.get("/api/v1/admin/finance/overview/", params).json()["data"]
    oc = data["kpis"]["orders_count"]
    assert oc["total_paid"] == 3
    assert oc["actual_paid"] == 2
    assert oc["sponsor_slot"] == 1
    assert oc["single_book"] == 2
    assert oc["multi_book"] == 1
    assert data["kpis"]["gateway_charges"]["amount"] == "11.44"


@pytest.mark.django_db
def test_finance_gst_collected_nets_credit_notes(system_config):
    admin = _finance_admin()
    buyer = _member("+918080099200")
    now = timezone.now()
    d = timezone.localdate()

    order = _paid_order(buyer, "ORD-GST-NET", paid_at=now)
    inv = GSTInvoice.objects.create(
        order=order,
        invoice_number="INV-GST-NET-1",
        base_amount=Decimal("200"),
        cgst=Decimal("18"),
        sgst=Decimal("18"),
        total_gst=Decimal("36"),
        grand_total=Decimal("236"),
    )
    GSTInvoice.objects.filter(pk=inv.pk).update(created_at=now)
    rr = RefundRequest.objects.create(
        reference="RET-GST-NET-1",
        order=order,
        user=buyer,
        amount=Decimal("241.72"),
        status=RefundRequest.Status.APPROVED,
        approved_at=now,
    )
    cn = CreditNote.objects.create(
        gst_invoice=inv,
        refund_request=rr,
        credit_note_number="CN-FY2526-00001",
        base_amount=Decimal("200"),
        cgst=Decimal("18"),
        sgst=Decimal("18"),
        total_gst=Decimal("36"),
        grand_total=Decimal("236"),
    )
    CreditNote.objects.filter(pk=cn.pk).update(created_at=now)

    client = APIClient()
    client.force_authenticate(user=admin)
    data = client.get(
        "/api/v1/admin/finance/overview/",
        {"from": d.isoformat(), "to": d.isoformat()},
    ).json()["data"]
    gst = data["kpis"]["gst_collected"]
    assert gst["invoiced"] == "36.00"
    assert gst["credited"] == "36.00"
    assert gst["amount"] == "0.00"
    assert gst["credit_note_count"] == 1

    gst_report = client.get(
        "/api/v1/admin/gst-report/",
        {"from": d.isoformat(), "to": d.isoformat()},
    ).json()["data"]
    assert gst_report["invoiced"] == "36.00"
    assert gst_report["credited"] == "36.00"
    assert gst_report["collected"] == "0.00"
    assert gst_report["credit_note_count"] == 1


@pytest.mark.django_db
def test_finance_gst_credit_note_in_later_period(system_config):
    admin = _finance_admin()
    buyer = _member("+918080099201")
    sale_day = timezone.localdate() - timedelta(days=10)
    refund_day = timezone.localdate()
    sale_at = timezone.make_aware(datetime.combine(sale_day, time.min))
    refund_at = timezone.now()

    order = _paid_order(buyer, "ORD-GST-PERIOD", paid_at=sale_at)
    inv = GSTInvoice.objects.create(
        order=order,
        invoice_number="INV-GST-PERIOD-1",
        base_amount=Decimal("200"),
        cgst=Decimal("18"),
        sgst=Decimal("18"),
        total_gst=Decimal("36"),
        grand_total=Decimal("236"),
    )
    GSTInvoice.objects.filter(pk=inv.pk).update(created_at=sale_at)
    rr = RefundRequest.objects.create(
        reference="RET-GST-PERIOD-1",
        order=order,
        user=buyer,
        amount=Decimal("241.72"),
        status=RefundRequest.Status.APPROVED,
        approved_at=refund_at,
    )
    cn = CreditNote.objects.create(
        gst_invoice=inv,
        refund_request=rr,
        credit_note_number="CN-FY2526-00002",
        base_amount=Decimal("200"),
        cgst=Decimal("18"),
        sgst=Decimal("18"),
        total_gst=Decimal("36"),
        grand_total=Decimal("236"),
    )
    CreditNote.objects.filter(pk=cn.pk).update(created_at=refund_at)

    client = APIClient()
    client.force_authenticate(user=admin)

    sale_data = client.get(
        "/api/v1/admin/finance/overview/",
        {"from": sale_day.isoformat(), "to": sale_day.isoformat()},
    ).json()["data"]
    assert sale_data["kpis"]["gst_collected"]["amount"] == "36.00"
    assert sale_data["kpis"]["gst_collected"]["credited"] == "0.00"

    refund_data = client.get(
        "/api/v1/admin/finance/overview/",
        {"from": refund_day.isoformat(), "to": refund_day.isoformat()},
    ).json()["data"]
    assert refund_data["kpis"]["gst_collected"]["invoiced"] == "0.00"
    assert refund_data["kpis"]["gst_collected"]["credited"] == "36.00"
    assert refund_data["kpis"]["gst_collected"]["amount"] == "-36.00"


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
