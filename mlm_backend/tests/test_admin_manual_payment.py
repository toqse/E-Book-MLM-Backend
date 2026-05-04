from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.courses.models import EBook
from apps.payments.models import Order
from apps.users.models import User
from apps.users.services import allocate_member_identity


def _super_admin():
    return User.objects.create_user(
        login_identifier="manual-pay-admin@test.dev",
        password="pw",
        email="manual-pay-admin@test.dev",
        full_name="Manual Pay Admin",
        member_id="MANPAY01",
        referral_code="MANPAY01",
        referral_link="http://localhost/join?ref=MANPAY01",
        role=User.Role.SUPER_ADMIN,
        is_staff=True,
    )


def _member():
    mid, ref, link = allocate_member_identity()
    u = User(
        phone="+919888877001",
        full_name="Buyer",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
    )
    u.set_unusable_password()
    u.save()
    return u


@pytest.fixture
def published_ebook(db):
    return EBook.objects.create(
        title="Book",
        slug="book-manual-pay",
        category="X",
        description="d",
        pages_count=1,
        language="English",
        price=200,
        status=EBook.Status.PUBLISHED,
        file_url="https://example.com/x.pdf",
        is_primary=True,
        is_active=True,
    )


@pytest.mark.django_db
def test_admin_manual_verify_marks_paid_and_opens_placement_queue(system_config, published_ebook):
    admin = _super_admin()
    buyer = _member()
    order = Order.objects.create(
        user=buyer,
        ebook=published_ebook,
        order_number="ORD-MANUAL-VERIFY-001",
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("241.72"),
        is_retail_purchase=False,
        status=Order.Status.CREATED,
        razorpay_order_id="order_test_manual_1",
    )
    client = APIClient()
    client.force_authenticate(user=admin)
    r = client.post(
        f"/api/v1/admin/orders/{order.id}/verify-payment-manual/",
        {"note": "test manual"},
        format="json",
    )
    assert r.status_code == 200, r.content
    data = r.json()["data"]
    assert data["status"] == "PAID"
    assert data["order_id"] == order.id
    order.refresh_from_db()
    assert order.status == Order.Status.PAID
    assert order.placement_status == Order.PlacementStatus.PENDING
    assert order.razorpay_payment_id.startswith("MANUAL-")
    assert AuditLog.objects.filter(action="payment.admin_verified", target_id=str(order.id)).exists()
    buyer.refresh_from_db()
    assert buyer.is_member is True

    r2 = client.post(f"/api/v1/admin/orders/{order.id}/verify-payment-manual/", {}, format="json")
    assert r2.status_code == 200
    assert "already" in (r2.json().get("message") or "").lower()

    # order_number in path (not only integer pk)
    r3 = client.post(
        f"/api/v1/admin/orders/{order.order_number}/verify-payment-manual/",
        {},
        format="json",
    )
    assert r3.status_code == 200
    assert "already" in (r3.json().get("message") or "").lower()


@pytest.mark.django_db
def test_member_cannot_call_admin_manual_verify(system_config, published_ebook):
    buyer = _member()
    order = Order.objects.create(
        user=buyer,
        ebook=published_ebook,
        order_number="ORD-MANUAL-VERIFY-002",
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("241.72"),
        is_retail_purchase=False,
        status=Order.Status.CREATED,
        razorpay_order_id="order_test_manual_2",
    )
    client = APIClient()
    client.force_authenticate(user=buyer)
    r = client.post(f"/api/v1/admin/orders/{order.id}/verify-payment-manual/", {}, format="json")
    assert r.status_code == 403


@pytest.mark.django_db
def test_admin_manual_verify_rejects_non_created(system_config, published_ebook):
    admin = _super_admin()
    buyer = _member()
    order = Order.objects.create(
        user=buyer,
        ebook=published_ebook,
        order_number="ORD-MANUAL-VERIFY-003",
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("241.72"),
        is_retail_purchase=False,
        status=Order.Status.REFUNDED,
        razorpay_order_id="order_test_manual_3",
    )
    client = APIClient()
    client.force_authenticate(user=admin)
    r = client.post(f"/api/v1/admin/orders/{order.id}/verify-payment-manual/", {}, format="json")
    assert r.status_code == 400
    assert "CREATED" in r.json().get("message", "")
