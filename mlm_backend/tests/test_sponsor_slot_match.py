"""Sponsor-slot codes are only usable by the issuer's direct referrals.

A slot code issued by user A may only be validated/redeemed by a user whose
registered sponsor is A (i.e. someone who joined using A's referral code).
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.courses.models import EBook
from apps.payments.models import Order
from apps.sponsor_slots.models import SponsorSlotBatch, SponsorSlotCode
from apps.sponsor_slots.services import SponsorSlotService
from apps.users.models import User
from apps.users.services import allocate_member_identity


@pytest.fixture
def fake_razorpay_client(monkeypatch):
    class OrderApi:
        @staticmethod
        def create(payload):
            return {"id": "rzord_fake_match", "amount": payload["amount"]}

    class Client:
        order = OrderApi()

    monkeypatch.setattr("apps.payments.services._client", lambda: Client())


def _member(phone: str, *, sponsor: User | None = None) -> User:
    mid, ref, link = allocate_member_identity()
    u = User(
        phone=phone,
        full_name="Slot Member",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        sponsor=sponsor,
        is_member=True,
    )
    u.set_unusable_password()
    u.save()
    return u


def _slot_code(
    issuer: User,
    code: str = "MATCHSLOT01",
    *,
    status: str = SponsorSlotCode.Status.ACTIVE,
    expires_at=None,
) -> SponsorSlotCode:
    expires_at = expires_at or (timezone.now() + timedelta(days=30))
    batch = SponsorSlotBatch.objects.create(
        issued_to=issuer,
        band_number=1,
        expires_at=expires_at,
    )
    return SponsorSlotCode.objects.create(
        batch=batch,
        issued_to=issuer,
        code=code,
        status=status,
        expires_at=expires_at,
    )


def _ebook(slug: str = "match-book") -> EBook:
    return EBook.objects.create(
        title="Match Book",
        slug=slug,
        category="Business",
        description="d",
        pages_count=10,
        language="English",
        price=Decimal("200"),
        status=EBook.Status.PUBLISHED,
        file_url="https://example.com/x.pdf",
        is_active=True,
    )


@pytest.mark.django_db
def test_validate_code_valid_for_direct_referral(system_config):
    issuer = _member("+918200000001")
    redeemer = _member("+918200000002", sponsor=issuer)
    _ebook()
    slot = _slot_code(issuer)
    client = APIClient()
    client.force_authenticate(user=redeemer)
    r = client.post(
        "/api/v1/sponsor-slots/validate/",
        {"sponsor_code": slot.code, "ebook_slug": "match-book"},
        format="json",
    )
    assert r.status_code == 200, r.content
    assert r.json()["data"]["valid"] is True


@pytest.mark.django_db
def test_validate_code_rejected_for_non_referral(system_config):
    issuer = _member("+918200000003")
    other_sponsor = _member("+918200000004")
    redeemer = _member("+918200000005", sponsor=other_sponsor)
    slot = _slot_code(issuer, code="MATCHSLOT02")
    client = APIClient()
    client.force_authenticate(user=redeemer)
    r = client.post(
        "/api/v1/sponsor-slots/validate/",
        {"sponsor_code": slot.code},
        format="json",
    )
    assert r.status_code == 400
    assert r.json()["data"]["reason"] == "not_your_sponsor"


@pytest.mark.django_db
def test_validate_code_rejected_for_sponsorless_redeemer(system_config):
    issuer = _member("+918200000006")
    redeemer = _member("+918200000007", sponsor=None)
    slot = _slot_code(issuer, code="MATCHSLOT03")
    result = SponsorSlotService.validate_code_detailed(slot.code, redeemer=redeemer)
    assert result.reason == "not_your_sponsor"
    assert result.valid is False


@pytest.mark.django_db
def test_self_redemption_still_invalid(system_config):
    issuer = _member("+918200000008")
    slot = _slot_code(issuer, code="MATCHSLOT04")
    result = SponsorSlotService.validate_code_detailed(slot.code, redeemer=issuer)
    assert result.reason == "invalid"


@pytest.mark.django_db
def test_expired_takes_precedence_over_not_your_sponsor(system_config):
    issuer = _member("+918200000009")
    other_sponsor = _member("+918200000010")
    redeemer = _member("+918200000011", sponsor=other_sponsor)
    slot = _slot_code(
        issuer,
        code="MATCHSLOT05",
        expires_at=timezone.now() - timedelta(days=1),
    )
    result = SponsorSlotService.validate_code_detailed(slot.code, redeemer=redeemer)
    assert result.reason == "expired"


@pytest.mark.django_db
def test_cart_checkout_no_discount_for_non_referral(system_config, fake_razorpay_client):
    issuer = _member("+918200000012")
    other_sponsor = _member("+918200000013")
    redeemer = _member("+918200000014", sponsor=other_sponsor)
    slot = _slot_code(issuer, code="MATCHSLOT06")
    _ebook(slug="match-book-cart")
    client = APIClient()
    client.force_authenticate(user=redeemer)
    assert (
        client.post(
            "/api/v1/user/cart/items/",
            {"ebook_slug": "match-book-cart"},
            format="json",
        ).status_code
        == 200
    )
    r = client.post(
        "/api/v1/user/cart/checkout/",
        {"sponsor_code": slot.code},
        format="json",
    )
    assert r.status_code == 200, r.content
    order = Order.objects.get(pk=r.json()["data"]["order_id"])
    assert order.discount_amount == Decimal("0")
    assert order.is_sponsor_slot_redemption is False


@pytest.mark.django_db
def test_cart_checkout_applies_discount_for_direct_referral(system_config, fake_razorpay_client):
    issuer = _member("+918200000015")
    redeemer = _member("+918200000016", sponsor=issuer)
    slot = _slot_code(issuer, code="MATCHSLOT07")
    _ebook(slug="match-book-cart2")
    client = APIClient()
    client.force_authenticate(user=redeemer)
    assert (
        client.post(
            "/api/v1/user/cart/items/",
            {"ebook_slug": "match-book-cart2"},
            format="json",
        ).status_code
        == 200
    )
    r = client.post(
        "/api/v1/user/cart/checkout/",
        {"sponsor_code": slot.code},
        format="json",
    )
    assert r.status_code == 200, r.content
    order = Order.objects.get(pk=r.json()["data"]["order_id"])
    assert order.discount_amount > Decimal("0")
    assert order.is_sponsor_slot_redemption is True
