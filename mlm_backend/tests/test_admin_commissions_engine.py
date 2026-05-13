"""Admin Commission Engine API (list, summary, detail, export, pending)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.commissions.models import CommissionLedger
from apps.payments.models import Order
from apps.users.models import User
from apps.users.services import allocate_member_identity


def _finance_admin() -> User:
    mid, ref, link = allocate_member_identity()
    return User.objects.create_user(
        login_identifier="fin-comm-admin@test.dev",
        password="pw",
        email="fin-comm-admin@test.dev",
        full_name="Finance Admin",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        role=User.Role.FINANCE,
        is_staff=True,
    )


def _member(phone: str, sponsor: User | None = None, full_name: str = "Member") -> User:
    mid, ref, link = allocate_member_identity()
    u = User(
        phone=phone,
        full_name=full_name,
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        sponsor=sponsor,
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
def test_admin_commissions_list_summary_detail_and_export(system_config):
    admin = _finance_admin()
    earner = _member("+918080080001", full_name="Ishaan Patel")
    buyer = _member("+918080080002", sponsor=earner, full_name="Buyer One")
    order = _paid_order(buyer, "ORD-ADM-COMM-1", razorpay_order_id="ord_59688")

    CommissionLedger.objects.create(
        recipient=earner,
        source_user=buyer,
        order=order,
        commission_type=CommissionLedger.CommissionType.DIRECT,
        amount=Decimal("30.00"),
        tds_deducted=Decimal("0.60"),
        net_amount=Decimal("29.40"),
        status=CommissionLedger.Status.CREDITED,
    )
    CommissionLedger.objects.create(
        recipient=earner,
        source_user=buyer,
        order=order,
        commission_type=CommissionLedger.CommissionType.UPLINE_L2,
        amount=Decimal("10.00"),
        tds_deducted=Decimal("0"),
        net_amount=Decimal("0"),
        status=CommissionLedger.Status.HELD,
    )
    CommissionLedger.objects.create(
        recipient=earner,
        source_user=buyer,
        order=order,
        commission_type=CommissionLedger.CommissionType.MILESTONE,
        amount=Decimal("500.00"),
        tds_deducted=Decimal("0"),
        net_amount=Decimal("500.00"),
        status=CommissionLedger.Status.CREDITED,
    )

    client = APIClient()
    client.force_authenticate(user=admin)

    r = client.get("/api/v1/admin/commissions/", {"page": 1, "page_size": 10})
    assert r.status_code == 200, r.content
    body = r.json()["data"]
    assert body["count"] == 2
    assert len(body["results"]) == 2
    types = {row["commission_type"] for row in body["results"]}
    assert CommissionLedger.CommissionType.MILESTONE not in types

    r_q = client.get("/api/v1/admin/commissions/", {"q": "ord_59688"})
    assert r_q.json()["data"]["count"] == 2

    r_l1 = client.get("/api/v1/admin/commissions/", {"level": "L1"})
    assert r_l1.json()["data"]["count"] == 1
    assert r_l1.json()["data"]["results"][0]["level"] == "L1"

    r_proc = client.get("/api/v1/admin/commissions/", {"status": "processed"})
    assert r_proc.json()["data"]["count"] == 1

    rs = client.get("/api/v1/admin/commissions/summary/")
    assert rs.status_code == 200
    sm = rs.json()["data"]
    assert sm["total_entries"] == 2
    assert sm["total_paid"] == "29.40"
    assert sm["pending"] == "0.00"
    assert "direct_commission_unit" in sm

    lid = r.json()["data"]["results"][0]["id"]
    rd = client.get(f"/api/v1/admin/commissions/{lid}/")
    assert rd.status_code == 200
    assert "order_detail" in rd.json()["data"]

    r404 = client.get("/api/v1/admin/commissions/999999/")
    assert r404.status_code == 404

    rp = client.get("/api/v1/admin/commissions/pending/")
    assert rp.status_code == 200
    pend = rp.json()["data"]["results"]
    assert len(pend) == 1
    assert pend[0]["status"] == CommissionLedger.Status.HELD

    csv_r = client.get("/api/v1/admin/commissions/export/", {"export_format": "csv"})
    assert csv_r.status_code == 200
    text = csv_r.content.decode("utf-8")
    assert "earner_member_id" in text
    assert earner.member_id in text

    pdf_r = client.get("/api/v1/admin/commissions/export/", {"export_format": "pdf"})
    assert pdf_r.status_code == 200
    pdf_bytes = pdf_r.content
    assert pdf_bytes[:4] == b"%PDF"

    bad = client.get("/api/v1/admin/commissions/export/", {"export_format": "xls"})
    assert bad.status_code == 400

    r_all = client.get(
        "/api/v1/admin/commissions/summary/",
        {"exclude_milestone": "false"},
    )
    assert r_all.json()["data"]["total_entries"] == 3
