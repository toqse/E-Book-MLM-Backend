"""Admin POST /api/v1/admin/commissions/force-credit/ — release HELD backlog."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.commissions.models import CommissionLedger
from apps.payments.models import Order
from apps.users.models import User
from apps.users.services import allocate_member_identity
from apps.wallet.models import Wallet


def _finance_admin() -> User:
    mid, ref, link = allocate_member_identity()
    return User.objects.create_user(
        login_identifier="held-rel-fin@test.dev",
        password="pw",
        email="held-rel-fin@test.dev",
        full_name="Finance Held",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        role=User.Role.FINANCE,
        is_staff=True,
    )


def _member(phone: str, sponsor: User | None = None, **kwargs) -> User:
    mid, ref, link = allocate_member_identity()
    u = User(
        phone=phone,
        full_name=kwargs.get("full_name", "Member"),
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        sponsor=sponsor,
        kyc_status=kwargs.get("kyc_status", User.KYCStatus.PENDING),
    )
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
        refund_eligible_until=now + timedelta(days=7),
    )
    defaults.update(kwargs)
    return Order.objects.create(**defaults)


@pytest.mark.django_db
def test_force_credit_404_unknown_user(system_config):
    admin = _finance_admin()
    client = APIClient()
    client.force_authenticate(user=admin)
    r = client.post("/api/v1/admin/commissions/force-credit/", {"user_id": 999999}, format="json")
    assert r.status_code == 404


@pytest.mark.django_db
def test_force_credit_400_bad_user_id(system_config):
    admin = _finance_admin()
    client = APIClient()
    client.force_authenticate(user=admin)
    r = client.post("/api/v1/admin/commissions/force-credit/", {}, format="json")
    assert r.status_code == 400


@pytest.mark.django_db
def test_force_credit_skips_when_kyc_not_verified(system_config):
    admin = _finance_admin()
    earner = _member("+917010010001", full_name="Earner")
    buyer = _member("+917010010002", sponsor=earner)
    order = _paid_order(buyer, "ORD-HELD-KYC")
    CommissionLedger.objects.create(
        recipient=earner,
        source_user=buyer,
        order=order,
        commission_type=CommissionLedger.CommissionType.DIRECT,
        amount=Decimal("30.00"),
        tds_deducted=Decimal("0"),
        net_amount=Decimal("0"),
        status=CommissionLedger.Status.HELD,
    )
    Wallet.objects.create(user=earner, cash_balance=Decimal("0"), total_earned=Decimal("0"))

    client = APIClient()
    client.force_authenticate(user=admin)
    r = client.post("/api/v1/admin/commissions/force-credit/", {"user_id": earner.pk}, format="json")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["credited_ids"] == []
    assert data["skipped"] and data["skipped"][0]["reason"] == "kyc_not_verified"
    row = CommissionLedger.objects.get(recipient=earner)
    assert row.status == CommissionLedger.Status.HELD


@pytest.mark.django_db
def test_force_credit_credits_after_kyc_verified(system_config):
    admin = _finance_admin()
    earner = _member("+917020020001", kyc_status=User.KYCStatus.VERIFIED, full_name="Verified Earner")
    buyer = _member("+917020020002", sponsor=earner)
    order = _paid_order(buyer, "ORD-HELD-OK")
    CommissionLedger.objects.create(
        recipient=earner,
        source_user=buyer,
        order=order,
        commission_type=CommissionLedger.CommissionType.DIRECT,
        amount=Decimal("30.00"),
        tds_deducted=Decimal("0"),
        net_amount=Decimal("0"),
        status=CommissionLedger.Status.HELD,
    )
    Wallet.objects.create(user=earner, cash_balance=Decimal("0"), total_earned=Decimal("0"))

    client = APIClient()
    client.force_authenticate(user=admin)
    r = client.post("/api/v1/admin/commissions/force-credit/", {"user_id": earner.pk}, format="json")
    assert r.status_code == 200
    data = r.json()["data"]
    assert len(data["credited_ids"]) == 1
    row = CommissionLedger.objects.get(pk=data["credited_ids"][0])
    assert row.status == CommissionLedger.Status.CREDITED
    assert row.net_amount > 0
    w = Wallet.objects.get(user=earner)
    assert w.total_earned == row.net_amount


@pytest.mark.django_db
def test_force_credit_skips_refunded_order(system_config):
    admin = _finance_admin()
    earner = _member("+917030030001", kyc_status=User.KYCStatus.VERIFIED)
    buyer = _member("+917030030002", sponsor=earner)
    order = _paid_order(buyer, "ORD-HELD-REF")
    order.status = Order.Status.REFUNDED
    order.save(update_fields=["status"])
    CommissionLedger.objects.create(
        recipient=earner,
        source_user=buyer,
        order=order,
        commission_type=CommissionLedger.CommissionType.DIRECT,
        amount=Decimal("30.00"),
        tds_deducted=Decimal("0"),
        net_amount=Decimal("0"),
        status=CommissionLedger.Status.HELD,
    )
    Wallet.objects.create(user=earner, cash_balance=Decimal("0"), total_earned=Decimal("0"))

    client = APIClient()
    client.force_authenticate(user=admin)
    r = client.post("/api/v1/admin/commissions/force-credit/", {"user_id": earner.pk}, format="json")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["credited_ids"] == []
    assert data["skipped"][0]["reason"] == "order_not_paid"


@pytest.mark.django_db
def test_force_credit_partial_cap_creates_remainder_held(system_config):
    """Only part of HELD gross fits under cap → remainder as new HELD row."""
    from apps.admin_panel.models import SystemConfig

    SystemConfig.objects.filter(pk=1).update(earning_cap=Decimal("100.00"))

    admin = _finance_admin()
    earner = _member("+917040040001", kyc_status=User.KYCStatus.VERIFIED)
    buyer = _member("+917040040002", sponsor=earner)
    order = _paid_order(buyer, "ORD-HELD-PART")

    Wallet.objects.create(
        user=earner,
        cash_balance=Decimal("90.00"),
        total_earned=Decimal("90.00"),
    )

    CommissionLedger.objects.create(
        recipient=earner,
        source_user=buyer,
        order=order,
        commission_type=CommissionLedger.CommissionType.DIRECT,
        amount=Decimal("30.00"),
        tds_deducted=Decimal("0"),
        net_amount=Decimal("0"),
        status=CommissionLedger.Status.HELD,
    )

    client = APIClient()
    client.force_authenticate(user=admin)
    r = client.post("/api/v1/admin/commissions/force-credit/", {"user_id": earner.pk}, format="json")
    assert r.status_code == 200
    data = r.json()["data"]
    assert len(data["credited_ids"]) == 1

    credited = CommissionLedger.objects.get(pk=data["credited_ids"][0])
    assert credited.status == CommissionLedger.Status.CREDITED

    remainder = CommissionLedger.objects.filter(recipient=earner, status=CommissionLedger.Status.HELD)
    assert remainder.count() == 1
    assert remainder.first().amount == Decimal("20.00")
