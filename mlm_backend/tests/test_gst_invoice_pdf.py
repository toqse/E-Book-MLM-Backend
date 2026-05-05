from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.courses.models import EBook
from apps.payments.models import GSTInvoice, Order, OrderLine
from apps.payments.services import (
    ensure_gst_invoice_pdf,
    finalize_order_as_paid,
    normalize_billing_from_payload,
)
from apps.users.models import User


@pytest.mark.django_db
def test_normalize_billing_truncates():
    data = {
        "billing_line1": "x" * 400,
        "billing_postal_code": "110001",
        "billing_unknown": "ignored",
    }
    out = normalize_billing_from_payload(data)
    assert len(out["billing_line1"]) == 255
    assert out["billing_postal_code"] == "110001"
    assert "billing_unknown" not in out


@pytest.mark.django_db
def test_finalize_order_creates_gst_invoice_pdf(system_config):
    user = User.objects.create_user(
        login_identifier="+919777777777",
        password="pw",
        full_name="Invoice Buyer",
        member_id="INVMBR01",
        referral_code="INV001",
        referral_link="http://localhost/join?ref=INV001",
        phone="+919777777777",
    )
    book = EBook.objects.create(
        title="Invoice Book",
        slug="invoice-book",
        category="X",
        description="d",
        pages_count=10,
        language="English",
        price=200,
        status=EBook.Status.PUBLISHED,
        file_url="https://example.com/b.pdf",
        is_active=True,
    )
    order = Order.objects.create(
        user=user,
        ebook=book,
        order_number="ORD-INV-PDF-TEST-1",
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("241.72"),
        status=Order.Status.CREATED,
        razorpay_order_id="ord_test_pdf",
        billing_line1="221B Baker Street",
        billing_city="Mumbai",
        billing_state="Maharashtra",
        billing_postal_code="400001",
        billing_country="India",
    )

    finalize_order_as_paid(order, payment_id="pay_test_pdf_abc")

    inv = GSTInvoice.objects.get(order=order)
    assert inv.invoice_number
    assert inv.pdf_file.name
    with inv.pdf_file.open("rb") as fh:
        assert fh.read(4) == b"%PDF"


@pytest.mark.django_db
def test_user_orders_lists_pdf_url(system_config):
    user = User.objects.create_user(
        login_identifier="+919666666666",
        password="pw",
        full_name="Orders List User",
        member_id="ORDLST01",
        referral_code="ORDL01",
        referral_link="http://localhost/join?ref=ORDL01",
        phone="+919666666666",
    )
    book = EBook.objects.create(
        title="List Book",
        slug="list-book",
        category="X",
        description="d",
        pages_count=5,
        language="English",
        price=200,
        status=EBook.Status.PUBLISHED,
        file_url="https://example.com/lb.pdf",
        is_active=True,
    )
    order = Order.objects.create(
        user=user,
        ebook=book,
        order_number="ORD-LIST-PDF-API-1",
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("241.72"),
        status=Order.Status.CREATED,
        razorpay_order_id="ord_list_test",
    )
    finalize_order_as_paid(order, payment_id="pay_list_test")

    client = APIClient()
    client.force_authenticate(user=user)
    r = client.get("/api/v1/user/orders/")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["success"] is True
    rows = body["data"]["results"]
    assert len(rows) >= 1
    hit = next(x for x in rows if x["order_number"] == "ORD-LIST-PDF-API-1")
    assert hit["status"] == "PAID"
    assert hit["purchased_at"] is not None
    assert hit["ebook_title"] == "List Book"
    assert hit["thumbnail_url"] is None
    assert hit["pdf_url"] is not None
    assert hit["pdf_url"].startswith("http://testserver/media/")


@pytest.mark.django_db
def test_ensure_gst_invoice_pdf_handles_none_name(system_config):
    user = User.objects.create_user(
        login_identifier="+919655555555",
        password="pw",
        full_name="None Name User",
        member_id="NONENAME1",
        referral_code="NNAME1",
        referral_link="http://localhost/join?ref=NNAME1",
        phone="+919655555555",
    )
    book = EBook.objects.create(
        title="NoneName Book",
        slug="none-name-book",
        category="X",
        description="d",
        pages_count=5,
        language="English",
        price=200,
        status=EBook.Status.PUBLISHED,
        file_url="https://example.com/nn.pdf",
        is_active=True,
    )
    order = Order.objects.create(
        user=user,
        ebook=book,
        order_number="ORD-NONE-NAME-1",
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("241.72"),
        status=Order.Status.CREATED,
        razorpay_order_id="ord_none_name",
    )
    finalize_order_as_paid(order, payment_id="pay_none_name")

    inv = GSTInvoice.objects.get(order=order)
    GSTInvoice.objects.filter(pk=inv.pk).update(pdf_file=None)
    inv.refresh_from_db()
    assert inv.pdf_file.name is None

    ensure_gst_invoice_pdf(order)
    inv.refresh_from_db()
    assert inv.pdf_file.name
    with inv.pdf_file.open("rb") as fh:
        assert fh.read(4) == b"%PDF"


@pytest.mark.django_db
def test_finalize_multiline_order_generates_pdf(system_config):
    user = User.objects.create_user(
        login_identifier="+919644444444",
        password="pw",
        full_name="Multi Line Buyer",
        member_id="MLINV01",
        referral_code="MLINV1",
        referral_link="http://localhost/join?ref=MLINV1",
        phone="+919644444444",
    )
    b1 = EBook.objects.create(
        title="Multi A",
        slug="multi-inv-a",
        category="X",
        description="d",
        pages_count=5,
        language="English",
        price=100,
        status=EBook.Status.PUBLISHED,
        file_url="https://example.com/a.pdf",
        is_active=True,
    )
    b2 = EBook.objects.create(
        title="Multi B",
        slug="multi-inv-b",
        category="X",
        description="d",
        pages_count=5,
        language="English",
        price=200,
        status=EBook.Status.PUBLISHED,
        file_url="https://example.com/b.pdf",
        is_active=True,
    )
    order = Order.objects.create(
        user=user,
        ebook=b1,
        order_number="ORD-MULTI-INV-1",
        base_price=Decimal("300"),
        gst_amount=Decimal("54"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("359.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("359.72"),
        status=Order.Status.CREATED,
        razorpay_order_id="ord_multi_inv",
    )
    OrderLine.objects.create(order=order, ebook=b1, unit_base_price=Decimal("100"))
    OrderLine.objects.create(order=order, ebook=b2, unit_base_price=Decimal("200"))
    finalize_order_as_paid(order, payment_id="pay_multi_inv")

    inv = GSTInvoice.objects.get(order=order)
    ensure_gst_invoice_pdf(order)
    inv.refresh_from_db()
    assert inv.pdf_file.name
    with inv.pdf_file.open("rb") as fh:
        assert fh.read(4) == b"%PDF"
