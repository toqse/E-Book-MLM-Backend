import pytest
from decimal import Decimal
from rest_framework.test import APIClient

from apps.cart.models import CartItem
from apps.courses.models import EBook, Enrollment
from apps.payments.models import Order, OrderLine
from apps.payments.services import finalize_order_as_paid
from apps.users.models import User


def _member(login_id: str, mid: str, ref: str, phone: str) -> User:
    return User.objects.create_user(
        login_identifier=login_id,
        password="pw",
        full_name="Cart Member",
        member_id=mid,
        referral_code=ref,
        referral_link=f"http://localhost/join?ref={ref}",
        phone=phone,
    )


def _book(slug: str, price: str | Decimal, title: str | None = None) -> EBook:
    return EBook.objects.create(
        title=title or slug,
        slug=slug,
        category="Business",
        description="d",
        pages_count=10,
        language="English",
        price=price,
        status=EBook.Status.PUBLISHED,
        file_url="https://example.com/x.pdf",
        is_active=True,
    )


@pytest.fixture
def fake_razorpay_client(monkeypatch):
    class OrderApi:
        @staticmethod
        def create(payload):
            return {"id": "rzord_fake_cart", "amount": payload["amount"]}

    class Client:
        order = OrderApi()

    monkeypatch.setattr("apps.payments.services._client", lambda: Client())


@pytest.mark.django_db
def test_cart_add_duplicate_returns_400(system_config):
    u = _member("+919811111111", "CARTM01", "CRT01", "+919811111111")
    b = _book("cart-dup", "100.00")
    c = APIClient()
    c.force_authenticate(user=u)
    r1 = c.post("/api/v1/user/cart/items/", {"ebook_slug": "cart-dup"}, format="json")
    assert r1.status_code == 200
    item = r1.json()["data"]["items"][0]
    assert "thumbnail_url" in item
    r2 = c.post("/api/v1/user/cart/items/", {"ebook_slug": "cart-dup"}, format="json")
    assert r2.status_code == 400
    assert "already in your cart" in (r2.json().get("message") or "").lower()


@pytest.mark.django_db
def test_cart_add_when_enrolled_returns_400(system_config):
    u = _member("+919822222222", "CARTM02", "CRT02", "+919822222222")
    b = _book("cart-owned", "150.00")
    o = Order.objects.create(
        user=u,
        ebook=b,
        order_number="ORD-CART-OWN-1",
        base_price=Decimal("150"),
        gst_amount=Decimal("27"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("182.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("182.72"),
        status=Order.Status.PAID,
    )
    Enrollment.objects.create(user=u, ebook=b, order=o, is_retail=False)
    c = APIClient()
    c.force_authenticate(user=u)
    r = c.post("/api/v1/user/cart/items/", {"ebook_slug": "cart-owned"}, format="json")
    assert r.status_code == 400
    assert "already enrolled" in (r.json().get("message") or "").lower()


@pytest.mark.django_db
def test_create_order_rejects_when_enrolled(system_config):
    u = _member("+919833333333", "CARTM03", "CRT03", "+919833333333")
    b = _book("direct-owned", "99.00")
    o = Order.objects.create(
        user=u,
        ebook=b,
        order_number="ORD-DIR-OWN-1",
        base_price=Decimal("99"),
        gst_amount=Decimal("17.82"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("122.54"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("122.54"),
        status=Order.Status.PAID,
    )
    Enrollment.objects.create(user=u, ebook=b, order=o, is_retail=False)
    c = APIClient()
    c.force_authenticate(user=u)
    r = c.post("/api/v1/payments/create-order/", {"ebook_slug": "direct-owned"}, format="json")
    assert r.status_code == 400


@pytest.mark.django_db
def test_cart_checkout_creates_lines_and_multi_enrollment(system_config, fake_razorpay_client):
    u = _member("+919844444444", "CARTM04", "CRT04", "+919844444444")
    b1 = _book("cart-a", "100.00")
    b2 = _book("cart-b", "200.00")
    c = APIClient()
    c.force_authenticate(user=u)
    assert c.post("/api/v1/user/cart/items/", {"ebook_slug": "cart-a"}, format="json").status_code == 200
    assert c.post("/api/v1/user/cart/items/", {"ebook_slug": "cart-b"}, format="json").status_code == 200
    chk = c.post("/api/v1/user/cart/checkout/", {}, format="json")
    assert chk.status_code == 200, chk.content
    data = chk.json()["data"]
    order_id = data["order_id"]
    order = Order.objects.get(pk=order_id)
    lines = list(OrderLine.objects.filter(order=order).order_by("ebook_id"))
    assert len(lines) == 2
    assert {lines[0].ebook_id, lines[1].ebook_id} == {b1.pk, b2.pk}
    assert CartItem.objects.filter(cart__user=u).count() == 2

    finalize_order_as_paid(order, payment_id="pay_cart_multi")
    assert CartItem.objects.filter(cart__user=u).count() == 0
    assert Enrollment.objects.filter(user=u, order=order).count() == 2


@pytest.mark.django_db
def test_cart_delete_clears(system_config):
    u = _member("+919855555555", "CARTM05", "CRT05", "+919855555555")
    b = _book("cart-clear", "50.00")
    c = APIClient()
    c.force_authenticate(user=u)
    c.post("/api/v1/user/cart/items/", {"ebook_slug": "cart-clear"}, format="json")
    r = c.delete("/api/v1/user/cart/")
    assert r.status_code == 200
    assert CartItem.objects.filter(cart__user=u).count() == 0


@pytest.mark.django_db
def test_cart_checkout_empty_returns_400(system_config):
    u = _member("+919866666666", "CARTM06", "CRT06", "+919866666666")
    c = APIClient()
    c.force_authenticate(user=u)
    r = c.post("/api/v1/user/cart/checkout/", {}, format="json")
    assert r.status_code == 400
