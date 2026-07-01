from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.agreements.models import MemberComplianceProfile
from apps.payments.models import Order
from apps.sponsor_slots.audit_log import log_sponsor_audit
from apps.sponsor_slots.models import SponsorSlotAuditEvent, SponsorSlotBatch, SponsorSlotCode
from apps.users.models import User
from apps.users.services import allocate_member_identity
from apps.wallet.models import Wallet, WithdrawalRequest


def _admin():
    return User.objects.create_user(
        login_identifier="dash-admin@test.dev",
        password="pw",
        email="dash-admin@test.dev",
        full_name="Dash Admin",
        member_id="DASHADM01",
        referral_code="DASH01",
        referral_link="http://localhost/join?ref=DASH01",
        role=User.Role.SUPER_ADMIN,
        is_staff=True,
    )


def _member(phone: str, *, name: str = "Member") -> User:
    mid, ref, link = allocate_member_identity()
    u = User(
        phone=phone,
        full_name=name,
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        is_member=True,
    )
    u.set_unusable_password()
    u.save()
    return u


@pytest.mark.django_db
def test_admin_dashboard_requires_auth(system_config):
    client = APIClient()
    r = client.get("/api/v1/admin/dashboard/")
    assert r.status_code == 401


@pytest.mark.django_db
def test_admin_dashboard_requires_admin_role(system_config, member_user):
    client = APIClient()
    client.force_authenticate(user=member_user)
    r = client.get("/api/v1/admin/dashboard/")
    assert r.status_code == 403


@pytest.mark.django_db
def test_admin_dashboard_shape_and_top_earners_order(system_config):
    admin = _admin()
    m_lo = _member("+919811111111", name="Low Earner")
    m_hi = _member("+919822222222", name="High Earner")

    MemberComplianceProfile.objects.create(user=m_hi, state="Delhi")

    Wallet.objects.create(user=m_lo, total_earned=Decimal("100.00"), current_band=1)
    Wallet.objects.create(user=m_hi, total_earned=Decimal("5000.00"), current_band=3)

    m_lo.direct_referral_count = 2
    m_lo.save(update_fields=["direct_referral_count"])
    m_hi.direct_referral_count = 5
    m_hi.save(update_fields=["direct_referral_count"])

    WithdrawalRequest.objects.create(
        user=m_hi,
        band=1,
        amount_requested=Decimal("1000.00"),
        net_payable=Decimal("900.00"),
        status=WithdrawalRequest.Status.PENDING,
    )

    batch = SponsorSlotBatch.objects.create(
        issued_to=m_hi,
        band_number=1,
        expires_at=timezone.now() + timedelta(days=30),
    )
    code = SponsorSlotCode.objects.create(
        batch=batch,
        issued_to=m_hi,
        code="DASHTEST01",
        status=SponsorSlotCode.Status.ACTIVE,
        expires_at=batch.expires_at,
    )
    log_sponsor_audit(code, SponsorSlotAuditEvent.EventType.ISSUED, actor=admin)

    Order.objects.create(
        user=m_lo,
        order_number="ORD-DASH-001",
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("241.72"),
        is_retail_purchase=False,
        status=Order.Status.PAID,
        paid_at=timezone.now(),
        refund_eligible_until=timezone.now() + timedelta(days=7),
    )

    client = APIClient()
    client.force_authenticate(user=admin)
    r = client.get("/api/v1/admin/dashboard/")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    data = body["data"]

    assert "summary_cards" in data
    assert len(data["summary_cards"]) == 6
    ids = [c["id"] for c in data["summary_cards"]]
    assert ids == [
        "total_members",
        "active_today",
        "book_sales_24h",
        "pending_payouts",
        "active_slots",
        "compliance_score",
    ]
    for c in data["summary_cards"]:
        assert "label" in c and "value" in c and "compare_label" in c

    rs = data["revenue_series"]
    assert rs["granularity"] in ("daily", "weekly", "monthly")
    assert "date_from" in rs and "date_to" in rs and "points" in rs
    assert isinstance(rs["points"], list)

    assert isinstance(data["sponsor_slot_activity"], list)
    assert data["sponsor_slot_activity"]
    assert data["sponsor_slot_activity"][0]["code"] == "DASHTEST01"

    assert isinstance(data["recent_joiners"], list)
    assert any(j.get("state") == "Delhi" for j in data["recent_joiners"])

    te = data["top_earners"]
    assert len(te) >= 2
    assert te[0]["member_id"] == m_hi.member_id
    assert te[0]["earnings"] == "5000.00"
    assert te[-1]["member_id"] == m_lo.member_id

    ts = data.get("top_sponsors")
    assert isinstance(ts, list)
    assert len(ts) >= 2
    assert ts[0]["member_id"] == m_hi.member_id
    assert ts[0]["direct_referrals"] == 5

    assert "total_members" in data
    assert "new_orders_today" in data
    assert "pending_withdrawals" in data
    assert data["pending_withdrawals"] >= 1
    assert "pending_kyc" in data


@pytest.mark.django_db
def test_admin_dashboard_revenue_granularity_weekly(system_config):
    admin = _admin()
    client = APIClient()
    client.force_authenticate(user=admin)
    r = client.get("/api/v1/admin/dashboard/?preset=7d&revenue_granularity=weekly")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["revenue_series"]["granularity"] == "weekly"
    assert len(data["revenue_series"]["points"]) >= 1
