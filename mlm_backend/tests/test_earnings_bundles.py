from decimal import Decimal

import pytest
from django.db import connection
from django.db.models import Sum
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from apps.admin_panel.models import SystemConfig
from apps.agreements.models import MemberComplianceProfile
from apps.commissions.engine import CommissionEngine
from apps.commissions.models import CommissionLedger
from apps.mlm_tree.services import BinaryTreeService
from apps.payments.models import Order
from apps.users.models import User
from apps.users.services import allocate_member_identity
from apps.wallet.models import Wallet
from apps.wallet.services.member_money import build_commissions_summary, get_wallet_row


def _three_level_tree():
    """Root -> sponsor -> buyer (binary); passive credits go to root."""
    mid_r, r_r, l_r = allocate_member_identity()
    root = User(
        phone="+918000000001",
        full_name="Root",
        member_id=mid_r,
        referral_code=r_r,
        referral_link=l_r,
        pan_number="ABCDE1234F",
        kyc_status=User.KYCStatus.VERIFIED,
    )
    root.set_unusable_password()
    root.save()
    root.is_member = True
    root.save()
    BinaryTreeService.place_member(root, None)

    mid_s, r_s, l_s = allocate_member_identity()
    sponsor = User(
        phone="+918000000002",
        full_name="S",
        member_id=mid_s,
        referral_code=r_s,
        referral_link=l_s,
        sponsor=root,
        pan_number="ABCDE1234F",
        kyc_status=User.KYCStatus.VERIFIED,
    )
    sponsor.set_unusable_password()
    sponsor.save()
    sponsor.is_member = True
    sponsor.save()
    BinaryTreeService.place_member(sponsor, root)

    mid_b, r_b, l_b = allocate_member_identity()
    buyer = User(
        phone="+918000000003",
        full_name="B",
        member_id=mid_b,
        referral_code=r_b,
        referral_link=l_b,
        sponsor=sponsor,
        pan_number="ABCDE1234F",
        kyc_status=User.KYCStatus.VERIFIED,
    )
    buyer.set_unusable_password()
    buyer.save()
    buyer.is_member = True
    buyer.save()
    BinaryTreeService.place_member(buyer, sponsor)
    for u in (root, sponsor, buyer):
        MemberComplianceProfile.objects.get_or_create(user=u)
    return root, sponsor, buyer


@pytest.mark.django_db
def test_commissions_summary_tree_passive_not_zero(system_config):
    root, sponsor, buyer = _three_level_tree()

    order = Order.objects.create(
        user=buyer,
        order_number="ORD-EARN-1",
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("241.72"),
        is_retail_purchase=False,
        status=Order.Status.PAID,
        paid_at=timezone.now(),
        refund_eligible_until=timezone.now() - timezone.timedelta(days=1),
    )
    CommissionEngine.process_order(order)

    passive = CommissionLedger.objects.filter(
        recipient=root,
        commission_type__startswith="UPLINE",
        status=CommissionLedger.Status.CREDITED,
    ).aggregate(s=Sum("net_amount"))["s"]
    assert (passive or Decimal("0")) > 0

    cfg = SystemConfig.objects.get(pk=1)
    wallet = get_wallet_row(root)
    summary = build_commissions_summary(root, cfg, wallet)
    assert summary["tree_passive"] != "0.00"
    assert Decimal(summary["tree_passive"]) == (passive or Decimal("0"))


@pytest.mark.django_db
def test_user_earnings_overview_and_ledger(system_config):
    root, sponsor, buyer = _three_level_tree()

    order = Order.objects.create(
        user=buyer,
        order_number="ORD-EARN-2",
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("241.72"),
        is_retail_purchase=False,
        status=Order.Status.PAID,
        paid_at=timezone.now(),
        refund_eligible_until=timezone.now() - timezone.timedelta(days=1),
    )
    CommissionEngine.process_order(order)

    client = APIClient()
    client.force_authenticate(user=sponsor)
    r = client.get("/api/v1/user/earnings/?include=overview,ledger&page_size=5")
    assert r.status_code == 200
    data = r.json()["data"]
    assert "summary" in data
    assert "ledger" in data
    w = data["summary"]["wallet"]
    assert set(w.keys()) == {"available_to_withdraw", "locked", "withdrawn"}
    assert data["summary"]["income"]["direct_l1"]["amount"] != "0.00"
    assert data["ledger"]["total_count"] >= 1
    assert len(data["ledger"]["rows"]) >= 1
    row0 = data["ledger"]["rows"][0]
    assert "balance" in row0
    assert "running_balance" in row0
    assert row0["balance"] == row0["running_balance"]
    assert "date" in row0 and "time" in row0
    assert "description" in row0 and row0["description"] == row0["detail"]
    assert "tds_deducted" in row0 and row0["tds_deducted"] == row0["tds"]
    assert "net_credited" in row0 and row0["net_credited"] == row0["net"]
    assert "status_label" in row0
    assert "via_downline" in row0
    assert "at" in row0
    assert "kind" in row0


@pytest.mark.django_db
def test_user_payouts_bundle_ladder_length(system_config):
    mid_s, r_s, l_s = allocate_member_identity()
    sponsor = User(
        phone="+918000000020",
        full_name="S3",
        member_id=mid_s,
        referral_code=r_s,
        referral_link=l_s,
    )
    sponsor.set_unusable_password()
    sponsor.save()
    sponsor.is_member = True
    sponsor.save()
    BinaryTreeService.place_member(sponsor, None)
    sponsor.kyc_status = User.KYCStatus.VERIFIED
    sponsor.save(update_fields=["kyc_status"])
    MemberComplianceProfile.objects.create(user=sponsor)
    Wallet.objects.filter(user=sponsor).update(total_earned=Decimal("5000"))
    User.objects.filter(pk=sponsor.pk).update(
        bank_account_number="123456789012",
        bank_ifsc="hdfc0001234",
        bank_name="HDFC Bank",
        upi_id="member@okhdfcbank",
    )
    sponsor.refresh_from_db()

    client = APIClient()
    client.force_authenticate(user=sponsor)
    r = client.get("/api/v1/user/payouts/?movements=true")
    assert r.status_code == 200
    body = r.json()["data"]
    assert len(body["bands"]) == 9
    assert "recent_movements" in body
    assert "bank_details" in body
    assert body["bank_details"]["account_number"] == "XXXX9012"
    assert body["bank_details"]["ifsc"] == "HDFC0001234"
    assert body["bank_details"]["bank_name"] == "HDFC Bank"
    assert body["upi_id"] == "member@okhdfcbank"

    User.objects.filter(pk=sponsor.pk).update(upi_id="")
    sponsor.refresh_from_db()
    client = APIClient()
    client.force_authenticate(user=sponsor)
    r2 = client.get("/api/v1/user/payouts/")
    assert r2.json()["data"]["upi_id"] is None


@pytest.mark.django_db
def test_earnings_bundle_query_budget(system_config):
    root, sponsor, buyer = _three_level_tree()
    order = Order.objects.create(
        user=buyer,
        order_number="ORD-EARN-3",
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("241.72"),
        is_retail_purchase=False,
        status=Order.Status.PAID,
        paid_at=timezone.now(),
        refund_eligible_until=timezone.now() - timezone.timedelta(days=1),
    )
    CommissionEngine.process_order(order)

    client = APIClient()
    client.force_authenticate(user=root)
    with CaptureQueriesContext(connection) as ctx:
        client.get("/api/v1/user/earnings/?include=overview,ledger")
    assert len(ctx.captured_queries) <= 28


@pytest.mark.django_db
def test_payouts_bundle_query_budget(system_config):
    mid_s, r_s, l_s = allocate_member_identity()
    sponsor = User(
        phone="+918000000040",
        full_name="S5",
        member_id=mid_s,
        referral_code=r_s,
        referral_link=l_s,
    )
    sponsor.set_unusable_password()
    sponsor.save()
    sponsor.is_member = True
    sponsor.save()
    BinaryTreeService.place_member(sponsor, None)
    sponsor.kyc_status = User.KYCStatus.VERIFIED
    sponsor.save(update_fields=["kyc_status"])
    MemberComplianceProfile.objects.create(user=sponsor)

    client = APIClient()
    client.force_authenticate(user=sponsor)
    with CaptureQueriesContext(connection) as ctx:
        client.get("/api/v1/user/payouts/?movements=true")
    assert len(ctx.captured_queries) <= 22
