import hashlib
import hmac
import json
from decimal import Decimal

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.commissions.models import CommissionLedger
from apps.courses.models import EBook, Enrollment
from apps.payments.models import Order
from apps.users.models import User
from apps.users.services import allocate_member_identity

WEBHOOK_SECRET = "test_webhook_secret_key"


def _member():
    mid, ref, link = allocate_member_identity()
    u = User(
        phone="+919877766001",
        full_name="Webhook Buyer",
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
        title="Webhook Book",
        slug="webhook-book",
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


def _created_order(buyer, ebook, *, rz_order_id="order_wh_test_1", amount=Decimal("241.72")):
    return Order.objects.create(
        user=buyer,
        ebook=ebook,
        order_number="ORD-WH-TEST-001",
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=amount,
        discount_amount=Decimal("0"),
        amount_paid=amount,
        is_retail_purchase=False,
        status=Order.Status.CREATED,
        razorpay_order_id=rz_order_id,
    )


def _captured_payload(
    *,
    rz_order_id: str = "order_wh_test_1",
    payment_id: str = "pay_wh_test_1",
    amount_paise: int = 24172,
):
    return {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": rz_order_id,
                    "amount": amount_paise,
                    "status": "captured",
                }
            }
        },
    }


def _sign_body(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post_webhook(client: APIClient, payload: dict, *, secret: str = WEBHOOK_SECRET):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return client.post(
        "/api/v1/payments/webhook/",
        data=body,
        content_type="application/json",
        HTTP_X_RAZORPAY_SIGNATURE=_sign_body(body, secret),
    )


@pytest.fixture
def webhook_client(settings):
    settings.RAZORPAY_KEY_SECRET = WEBHOOK_SECRET
    return APIClient()


@pytest.mark.django_db
@override_settings(RAZORPAY_KEY_SECRET=WEBHOOK_SECRET)
def test_webhook_payment_captured_finalizes_created_order(
    system_config, published_ebook, webhook_client
):
    buyer = _member()
    order = _created_order(buyer, published_ebook)

    r = _post_webhook(webhook_client, _captured_payload())
    assert r.status_code == 200, r.content
    data = r.json()["data"]
    assert data["received"] is True
    assert data["action"] == "finalized"
    assert data["order_id"] == order.id

    order.refresh_from_db()
    assert order.status == Order.Status.PAID
    assert order.razorpay_payment_id == "pay_wh_test_1"
    assert order.placement_status == Order.PlacementStatus.PENDING
    assert Enrollment.objects.filter(user=buyer, ebook=published_ebook, order=order).exists()
    assert AuditLog.objects.filter(action="payment.webhook_captured", target_id=str(order.id)).exists()


@pytest.mark.django_db
@override_settings(RAZORPAY_KEY_SECRET=WEBHOOK_SECRET)
def test_webhook_duplicate_is_idempotent(system_config, published_ebook, webhook_client):
    buyer = _member()
    order = _created_order(buyer, published_ebook)
    payload = _captured_payload()

    assert _post_webhook(webhook_client, payload).status_code == 200
    r2 = _post_webhook(webhook_client, payload)
    assert r2.status_code == 200
    assert r2.json()["data"]["action"] == "already_paid"
    assert Enrollment.objects.filter(order=order).count() == 1


@pytest.mark.django_db
@override_settings(RAZORPAY_KEY_SECRET=WEBHOOK_SECRET)
def test_webhook_then_verify_no_duplicate_enrollment(
    system_config, published_ebook, webhook_client, monkeypatch
):
    class Utility:
        @staticmethod
        def verify_payment_signature(params):
            return None

    class Client:
        utility = Utility()

    monkeypatch.setattr("apps.payments.services._client", lambda: Client())

    buyer = _member()
    order = _created_order(buyer, published_ebook, rz_order_id="order_wh_race_1")

    assert _post_webhook(
        webhook_client,
        _captured_payload(rz_order_id="order_wh_race_1", payment_id="pay_wh_race_1"),
    ).status_code == 200

    api = APIClient()
    api.force_authenticate(user=buyer)
    vr = api.post(
        "/api/v1/payments/verify/",
        {
            "order_id": order.id,
            "razorpay_payment_id": "pay_wh_race_1",
            "razorpay_signature": "sig_ok",
        },
        format="json",
    )
    assert vr.status_code == 200, vr.content
    assert Enrollment.objects.filter(order=order).count() == 1
    assert CommissionLedger.objects.filter(order=order).count() == 0


@pytest.mark.django_db
@override_settings(RAZORPAY_KEY_SECRET=WEBHOOK_SECRET)
def test_webhook_amount_mismatch_skips_finalize(system_config, published_ebook, webhook_client):
    buyer = _member()
    order = _created_order(buyer, published_ebook)

    r = _post_webhook(webhook_client, _captured_payload(amount_paise=100))
    assert r.status_code == 200
    assert r.json()["data"]["action"] == "skipped"
    assert r.json()["data"]["reason"] == "amount_mismatch"

    order.refresh_from_db()
    assert order.status == Order.Status.CREATED


@pytest.mark.django_db
@override_settings(RAZORPAY_KEY_SECRET=WEBHOOK_SECRET)
def test_webhook_unknown_order_skips(system_config, webhook_client):
    r = _post_webhook(
        webhook_client,
        _captured_payload(rz_order_id="order_does_not_exist", payment_id="pay_orphan"),
    )
    assert r.status_code == 200
    assert r.json()["data"]["action"] == "skipped"
    assert r.json()["data"]["reason"] == "order_not_found"


@pytest.mark.django_db
@override_settings(RAZORPAY_KEY_SECRET=WEBHOOK_SECRET)
def test_webhook_bad_signature_returns_400(webhook_client):
    body = json.dumps(_captured_payload()).encode("utf-8")
    r = webhook_client.post(
        "/api/v1/payments/webhook/",
        data=body,
        content_type="application/json",
        HTTP_X_RAZORPAY_SIGNATURE="bad_signature",
    )
    assert r.status_code == 400


@pytest.mark.django_db
@override_settings(RAZORPAY_KEY_SECRET=WEBHOOK_SECRET)
def test_webhook_refund_event_acknowledged_only(system_config, webhook_client):
    payload = {
        "event": "refund.processed",
        "payload": {"refund": {"entity": {"id": "rfnd_x"}}},
    }
    r = _post_webhook(webhook_client, payload)
    assert r.status_code == 200
    assert r.json()["data"]["action"] == "acknowledged"
