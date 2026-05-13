from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.commissions.models import CommissionLedger
from apps.courses.models import EBook, Enrollment
from apps.payments import refund_request_views as refund_views
from apps.payments.models import Order, OrderLine, RefundRequest
from apps.payments.services import RazorpayRefundError, finalize_order_as_paid
from apps.users.models import User
from apps.users.services import allocate_member_identity
from apps.wallet.models import Wallet


def _finance_admin():
    return User.objects.create_user(
        login_identifier="finance-ref@test.dev",
        password="pw",
        email="finance-ref@test.dev",
        full_name="Finance Refund",
        member_id="FINREFRQ",
        referral_code="FINREFRQ",
        referral_link="http://localhost/join?ref=FINREFRQ",
        role=User.Role.FINANCE,
        is_staff=True,
    )


def _buyer(*, sponsor=None, phone=None):
    mid, ref, link = allocate_member_identity()
    u = User(
        phone=phone or "+918080801001",
        full_name="Refund Buyer",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        sponsor=sponsor,
    )
    u.set_unusable_password()
    u.save()
    return u


def _published_ebook():
    return EBook.objects.create(
        title="Refund Book",
        slug="refund-book",
        category="X",
        description="d",
        pages_count=1,
        language="English",
        price=200,
        status=EBook.Status.PUBLISHED,
        file_url="https://example.com/r.pdf",
        is_primary=False,
        is_active=True,
    )


def _paid_order_with_window(user: User, ebook: EBook):
    now = timezone.now()
    o = Order.objects.create(
        user=user,
        ebook=ebook,
        order_number=f"ORD-RR-{user.id}-{now.timestamp()}",
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("241.72"),
        is_retail_purchase=False,
        status=Order.Status.CREATED,
        razorpay_order_id="ord_rr_test",
    )
    finalize_order_as_paid(o, payment_id="pay_rr_gateway")
    o.refresh_from_db()
    assert o.refund_eligible_until
    assert o.refund_eligible_until > now
    return o


def _paid_order_direct_window(user: User, ebook: EBook):
    now = timezone.now()
    o = Order.objects.create(
        user=user,
        ebook=ebook,
        order_number=f"ORD-RR-D-{user.id}-{now.timestamp()}",
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("241.72"),
        is_retail_purchase=False,
        status=Order.Status.CREATED,
        razorpay_order_id="ord_rr_direct",
    )
    finalize_order_as_paid(o, payment_id="MANUAL-77-abcd1234ef5678")
    o.refresh_from_db()
    assert o.razorpay_payment_id.startswith("MANUAL-")
    return o


@pytest.fixture
def razorpay_refund_stub_ok(monkeypatch):
    def fake(order, *, refund_reference, amount_inr):
        return "rfnd_stub_ok"

    monkeypatch.setattr(refund_views, "refund_razorpay_payment_for_order", fake)


@pytest.mark.django_db
def test_member_submits_refund_request_and_orders_list_shows_placeholder(
    system_config,
    db,
    razorpay_refund_stub_ok,
):
    buyer = _buyer()
    ebook = _published_ebook()
    order = _paid_order_with_window(buyer, ebook)

    client = APIClient()
    client.force_authenticate(user=buyer)

    r0 = client.get("/api/v1/user/orders/")
    hit0 = next(x for x in r0.json()["data"]["results"] if x["id"] == order.id)
    assert hit0.get("can_refund") is True

    r = client.post(f"/api/v1/user/orders/{order.pk}/refund/", {}, format="json")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["success"] is True
    data = body["data"]
    assert data["reference"].startswith(f"RET-{timezone.now().year}-{order.pk}-")
    assert data["status"] == "PENDING"
    rr = RefundRequest.objects.get(order=order)
    assert rr.amount == order.amount_paid

    r2 = client.get("/api/v1/user/orders/")
    rows = r2.json()["data"]["results"]
    hit = next(x for x in rows if x["id"] == order.id)
    assert hit["refund_request"]["reference"] == rr.reference
    assert hit["refund_request"]["status"] == "PENDING"
    assert hit["refund_request"].get("order_line_id") is None
    assert hit.get("can_refund") is False

    r_list = client.get("/api/v1/user/refund-requests/")
    assert r_list.status_code == 200
    lst = r_list.json()["data"]["results"]
    assert any(x["reference"] == rr.reference for x in lst)

    fac = APIClient()
    fac.force_authenticate(user=_finance_admin())
    summ = fac.get("/api/v1/admin/refunds/summary/")
    assert summ.status_code == 200
    s = summ.json()["data"]
    assert s["pending_review"] >= 1

    appr = fac.post(f"/api/v1/admin/refunds/{rr.id}/approve/", {}, format="json")
    assert appr.status_code == 200, appr.content
    assert appr.json()["data"].get("razorpay_refund_id") == "rfnd_stub_ok"
    rr.refresh_from_db()
    assert rr.razorpay_refund_id == "rfnd_stub_ok"
    order.refresh_from_db()
    assert order.status == Order.Status.REFUNDED
    assert not Enrollment.objects.filter(order=order).exists()

    r3 = client.get("/api/v1/user/orders/")
    hit3 = next(x for x in r3.json()["data"]["results"] if x["id"] == order.id)
    assert hit3["status"] == Order.Status.REFUNDED
    assert hit3.get("can_refund") is False
    assert hit3["refund_request"]["status"] == "APPROVED"
    assert hit3["refund_request"]["reference"] == rr.reference

    dup = client.post(f"/api/v1/user/orders/{order.pk}/refund/", {}, format="json")
    assert dup.status_code == 400


@pytest.mark.django_db
def test_refund_request_rejected_and_second_request_allowed(system_config):
    buyer = _buyer()
    ebook = _published_ebook()
    order = _paid_order_with_window(buyer, ebook)
    admin = _finance_admin()

    client = APIClient()
    client.force_authenticate(user=buyer)
    client.post(f"/api/v1/user/orders/{order.pk}/refund/", {}, format="json")
    rr = RefundRequest.objects.get(order=order)

    fac = APIClient()
    fac.force_authenticate(user=admin)
    rj = fac.post(
        f"/api/v1/admin/refunds/{rr.id}/reject/",
        {"reason": "no"},
        format="json",
    )
    assert rj.status_code == 200
    order.refresh_from_db()
    assert order.status == Order.Status.PAID

    r2 = client.post(f"/api/v1/user/orders/{order.pk}/refund/", {}, format="json")
    assert r2.status_code == 200


@pytest.mark.django_db
def test_refund_submit_fails_window_expired(system_config):
    buyer = _buyer()
    ebook = _published_ebook()
    order = _paid_order_with_window(buyer, ebook)
    Order.objects.filter(pk=order.pk).update(
        refund_eligible_until=timezone.now() - timedelta(seconds=1)
    )

    client = APIClient()
    client.force_authenticate(user=buyer)
    r_list = client.get("/api/v1/user/orders/")
    hit = next(x for x in r_list.json()["data"]["results"] if x["id"] == order.id)
    assert hit.get("can_refund") is False

    r = client.post(f"/api/v1/user/orders/{order.pk}/refund/", {}, format="json")
    assert r.status_code == 400
    assert "window" in r.json().get("message", "").lower()


@pytest.mark.django_db
def test_refund_submit_duplicate_open(system_config):
    buyer = _buyer()
    ebook = _published_ebook()
    order = _paid_order_with_window(buyer, ebook)
    client = APIClient()
    client.force_authenticate(user=buyer)
    assert client.post(f"/api/v1/user/orders/{order.pk}/refund/", {}, format="json").status_code == 200
    r2 = client.post(f"/api/v1/user/orders/{order.pk}/refund/", {}, format="json")
    assert r2.status_code == 400


@pytest.mark.django_db
def test_admin_approve_reverses_commissions(system_config, razorpay_refund_stub_ok):
    sponsor = _buyer(phone="+918717171717")
    buyer = _buyer(sponsor=sponsor)
    ebook = _published_ebook()
    order = _paid_order_with_window(buyer, ebook)
    Wallet.objects.update_or_create(
        user=sponsor,
        defaults={
            "cash_balance": Decimal("30.00"),
            "total_earned": Decimal("30.00"),
        },
    )
    CommissionLedger.objects.create(
        recipient=sponsor,
        source_user=buyer,
        order=order,
        commission_type=CommissionLedger.CommissionType.DIRECT,
        amount=Decimal("30.00"),
        tds_deducted=Decimal("0"),
        net_amount=Decimal("30.00"),
        status=CommissionLedger.Status.CREDITED,
    )
    assert CommissionLedger.objects.filter(
        order=order, status=CommissionLedger.Status.CREDITED
    ).exists()

    client = APIClient()
    client.force_authenticate(user=buyer)
    client.post(f"/api/v1/user/orders/{order.pk}/refund/", {}, format="json")
    rr = RefundRequest.objects.get(order=order)

    fac = APIClient()
    fac.force_authenticate(user=_finance_admin())
    assert fac.post(f"/api/v1/admin/refunds/{rr.id}/approve/", {}, format="json").status_code == 200

    assert not CommissionLedger.objects.filter(
        order=order, status=CommissionLedger.Status.CREDITED
    ).exists()
    assert CommissionLedger.objects.filter(
        order=order, status=CommissionLedger.Status.REVERSED
    ).exists()
    w = Wallet.objects.get(user=sponsor)
    assert w.cash_balance == Decimal("0")


@pytest.mark.django_db
def test_mark_processing_and_admin_list_filter(system_config):
    buyer = _buyer()
    ebook = _published_ebook()
    order = _paid_order_with_window(buyer, ebook)
    client = APIClient()
    client.force_authenticate(user=buyer)
    client.post(f"/api/v1/user/orders/{order.pk}/refund/", {}, format="json")
    rr = RefundRequest.objects.get(order=order)

    fac = APIClient()
    fac.force_authenticate(user=_finance_admin())
    mp = fac.post(f"/api/v1/admin/refunds/{rr.id}/mark-processing/", {}, format="json")
    assert mp.status_code == 200
    rr.refresh_from_db()
    assert rr.status == RefundRequest.Status.PROCESSING

    lst = fac.get("/api/v1/admin/refunds/", {"status": "PROCESSING"})
    assert lst.status_code == 200
    ids = {x["id"] for x in lst.json()["data"]["results"]}
    assert rr.id in ids


@pytest.mark.django_db
def test_approve_direct_skips_razorpay_refund_id(system_config):
    """DIRECT/manual payments use internal fulfilment only (no Razorpay refund id)."""
    buyer = _buyer()
    ebook = _published_ebook()
    order = _paid_order_direct_window(buyer, ebook)
    client = APIClient()
    client.force_authenticate(user=buyer)
    client.post(f"/api/v1/user/orders/{order.pk}/refund/", {}, format="json")
    rr = RefundRequest.objects.get(order=order)
    assert rr.payment_method == RefundRequest.PaymentMethod.DIRECT

    fac = APIClient()
    fac.force_authenticate(user=_finance_admin())
    r = fac.post(f"/api/v1/admin/refunds/{rr.id}/approve/", {}, format="json")
    assert r.status_code == 200, r.content
    rr.refresh_from_db()
    assert rr.razorpay_refund_id is None


@pytest.mark.django_db
def test_approve_gateway_razorpay_error_leaves_order_paid(system_config, monkeypatch):
    def boom(order, *, refund_reference, amount_inr):
        raise RazorpayRefundError("gateway refused", status_code=502)

    monkeypatch.setattr(refund_views, "refund_razorpay_payment_for_order", boom)

    buyer = _buyer()
    ebook = _published_ebook()
    order = _paid_order_with_window(buyer, ebook)
    client = APIClient()
    client.force_authenticate(user=buyer)
    client.post(f"/api/v1/user/orders/{order.pk}/refund/", {}, format="json")
    rr = RefundRequest.objects.get(order=order)
    fac = APIClient()
    fac.force_authenticate(user=_finance_admin())
    resp = fac.post(f"/api/v1/admin/refunds/{rr.id}/approve/", {}, format="json")
    assert resp.status_code == 502
    order.refresh_from_db()
    assert order.status == Order.Status.PAID
    rr.refresh_from_db()
    assert rr.status == RefundRequest.Status.PENDING


def _published_ebook_pair():
    a = _published_ebook()
    b = EBook.objects.create(
        title="Refund Book B",
        slug="refund-book-b",
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
    return a, b


def _two_line_paid_order(user: User, eb1: EBook, eb2: EBook):
    now = timezone.now()
    o = Order.objects.create(
        user=user,
        ebook=eb1,
        order_number=f"ORD-RR2-{user.id}-{now.timestamp()}",
        base_price=Decimal("400"),
        gst_amount=Decimal("72"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("477.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("477.72"),
        is_retail_purchase=False,
        status=Order.Status.CREATED,
        razorpay_order_id="ord_rr2_test",
    )
    OrderLine.objects.create(order=o, ebook=eb1, unit_base_price=Decimal("200"))
    OrderLine.objects.create(order=o, ebook=eb2, unit_base_price=Decimal("200"))
    finalize_order_as_paid(o, payment_id="pay_rr2_gateway")
    o.refresh_from_db()
    return o


@pytest.mark.django_db
def test_multi_line_partial_refund_then_full_reverses_commissions(
    system_config,
    razorpay_refund_stub_ok,
):
    sponsor = _buyer(phone="+918181818181")
    buyer = _buyer(sponsor=sponsor)
    eb1, eb2 = _published_ebook_pair()
    order = _two_line_paid_order(buyer, eb1, eb2)
    assert Enrollment.objects.filter(order=order).count() == 2

    Wallet.objects.update_or_create(
        user=sponsor,
        defaults={
            "cash_balance": Decimal("30.00"),
            "total_earned": Decimal("30.00"),
        },
    )
    CommissionLedger.objects.create(
        recipient=sponsor,
        source_user=buyer,
        order=order,
        commission_type=CommissionLedger.CommissionType.DIRECT,
        amount=Decimal("30.00"),
        tds_deducted=Decimal("0"),
        net_amount=Decimal("30.00"),
        status=CommissionLedger.Status.CREDITED,
    )

    line1 = OrderLine.objects.filter(order=order, ebook=eb1).first()
    line2 = OrderLine.objects.filter(order=order, ebook=eb2).first()
    assert line1 and line2

    client = APIClient()
    client.force_authenticate(user=buyer)
    r_items = client.get("/api/v1/user/orders/")
    hit0 = next(x for x in r_items.json()["data"]["results"] if x["id"] == order.id)
    assert len(hit0["items"]) == 2

    r1 = client.post(
        f"/api/v1/user/orders/{order.pk}/refund/",
        {"order_line_id": line1.pk},
        format="json",
    )
    assert r1.status_code == 200, r1.content
    rr1 = RefundRequest.objects.get(order=order, order_line_id=line1.pk)
    assert rr1.amount == Decimal("238.86")

    fac = APIClient()
    fac.force_authenticate(user=_finance_admin())
    assert fac.post(f"/api/v1/admin/refunds/{rr1.id}/approve/", {}, format="json").status_code == 200

    order.refresh_from_db()
    assert order.status == Order.Status.PAID
    assert not Enrollment.objects.filter(order=order, ebook=eb1).exists()
    assert Enrollment.objects.filter(order=order, ebook=eb2).exists()
    assert CommissionLedger.objects.filter(
        order=order, status=CommissionLedger.Status.CREDITED
    ).exists()

    r2 = client.post(
        f"/api/v1/user/orders/{order.pk}/refund/",
        {"order_line_id": line2.pk},
        format="json",
    )
    assert r2.status_code == 200, r2.content
    rr2 = RefundRequest.objects.get(order=order, order_line_id=line2.pk)
    assert rr2.amount == Decimal("238.86")

    assert fac.post(f"/api/v1/admin/refunds/{rr2.id}/approve/", {}, format="json").status_code == 200
    order.refresh_from_db()
    assert order.status == Order.Status.REFUNDED
    assert not Enrollment.objects.filter(order=order).exists()
    assert not CommissionLedger.objects.filter(
        order=order, status=CommissionLedger.Status.CREDITED
    ).exists()
    w = Wallet.objects.get(user=sponsor)
    assert w.cash_balance == Decimal("0")
