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
from apps.commissions.models import CommissionLedger, MilestoneRecord
from apps.mlm_tree.services import BinaryTreeService
from apps.payments.models import Order
from apps.sponsor_slots.models import SponsorSlotBatch, SponsorSlotCode
from apps.users.models import User
from apps.users.services import allocate_member_identity
from tests.conftest import unique_test_pan
from apps.wallet.models import Wallet, WalletTransaction, WithdrawalRequest
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
        pan_number=unique_test_pan(),
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
        pan_number=unique_test_pan(),
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
        pan_number=unique_test_pan(),
        kyc_status=User.KYCStatus.VERIFIED,
    )
    buyer.set_unusable_password()
    buyer.save()
    buyer.is_member = True
    buyer.save()
    BinaryTreeService.place_member(buyer, sponsor)
    approved_at = timezone.now()
    for u in (root, sponsor, buyer):
        MemberComplianceProfile.objects.get_or_create(user=u)
        if not u.kyc_first_approved_at:
            u.kyc_first_approved_at = approved_at
            u.save(update_fields=["kyc_first_approved_at"])
    return root, sponsor, buyer


def _finance_admin() -> User:
    mid, ref, link = allocate_member_identity()
    return User.objects.create_user(
        login_identifier="earn-bundle-fin@test.dev",
        password="pw",
        email="earn-bundle-fin@test.dev",
        full_name="Finance Earn",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        role=User.Role.FINANCE,
        is_staff=True,
    )


def _prepare_sponsor_for_withdrawal(sponsor: User) -> None:
    SystemConfig.objects.filter(pk=1).update(cooling_off_days=0)
    Wallet.objects.filter(user=sponsor).update(
        cash_balance=Decimal("500"),
        current_band=1,
    )
    User.objects.filter(pk=sponsor.pk).update(upi_id="sponsor@okhdfcbank")
    sponsor.refresh_from_db()


def _paid_order_for_buyer(buyer: User, order_number: str) -> Order:
    return Order.objects.create(
        user=buyer,
        order_number=order_number,
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
    assert set(w.keys()) == {
        "available_to_withdraw",
        "locked",
        "withdrawn",
        "tds_payable_194r",
        "total_tds_deducted",
    }
    assert isinstance(w["withdrawn"], dict)
    assert set(w["withdrawn"].keys()) == {
        "total",
        "already_paid_out",
        "held_for_review",
    }
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
    sponsor.kyc_first_approved_at = timezone.now()
    sponsor.save(update_fields=["kyc_status", "kyc_first_approved_at"])
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
    assert isinstance(body["wallet"]["total_withdrawn"], dict)
    assert set(body["wallet"]["total_withdrawn"].keys()) == {
        "total",
        "already_paid_out",
        "held_for_review",
    }
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
def test_withdrawn_breakdown_buckets(system_config):
    _root, sponsor, _buyer = _three_level_tree()
    # Ensure wallet exists (builder reads wallet.total_withdrawn, not derived).
    wallet = get_wallet_row(sponsor)

    paid_net = Decimal("120.00")
    pending_net = Decimal("40.00")
    failed_net = Decimal("25.00")
    rejected_net = Decimal("999.00")

    # Seed withdrawal requests in various states.
    WithdrawalRequest.objects.create(
        user=sponsor,
        band=1,
        amount_requested=paid_net,
        tds_amount=Decimal("0.00"),
        net_payable=paid_net,
        tds_section="",
        payout_method=WithdrawalRequest.PayoutMethod.UPI,
        payout_destination_hint="sponsor@okhdfcbank",
        status=WithdrawalRequest.Status.PAID,
    )
    WithdrawalRequest.objects.create(
        user=sponsor,
        band=1,
        amount_requested=pending_net,
        tds_amount=Decimal("0.00"),
        net_payable=pending_net,
        tds_section="",
        payout_method=WithdrawalRequest.PayoutMethod.UPI,
        payout_destination_hint="sponsor@okhdfcbank",
        status=WithdrawalRequest.Status.PENDING,
    )
    WithdrawalRequest.objects.create(
        user=sponsor,
        band=1,
        amount_requested=failed_net,
        tds_amount=Decimal("0.00"),
        net_payable=failed_net,
        tds_section="",
        payout_method=WithdrawalRequest.PayoutMethod.UPI,
        payout_destination_hint="sponsor@okhdfcbank",
        status=WithdrawalRequest.Status.FAILED,
    )
    WithdrawalRequest.objects.create(
        user=sponsor,
        band=1,
        amount_requested=rejected_net,
        tds_amount=Decimal("0.00"),
        net_payable=rejected_net,
        tds_section="",
        payout_method=WithdrawalRequest.PayoutMethod.UPI,
        payout_destination_hint="sponsor@okhdfcbank",
        status=WithdrawalRequest.Status.REJECTED,
    )

    # wallet.total_withdrawn should exclude REJECTED (mirrors admin reject logic).
    wallet.total_withdrawn = paid_net + pending_net + failed_net
    wallet.save(update_fields=["total_withdrawn"])

    client = APIClient()
    client.force_authenticate(user=sponsor)
    r = client.get("/api/v1/user/earnings/?include=overview")
    assert r.status_code == 200
    payload = r.json()["data"]
    withdrawn = payload["summary"]["wallet"]["withdrawn"]

    assert Decimal(withdrawn["already_paid_out"]) == paid_net
    assert Decimal(withdrawn["held_for_review"]) == pending_net + failed_net
    assert Decimal(withdrawn["total"]) == paid_net + pending_net + failed_net

    # Same nested object must surface as `total_withdrawn` on /user/payouts/
    r2 = client.get("/api/v1/user/payouts/")
    assert r2.status_code == 200
    pwallet = r2.json()["data"]["wallet"]
    assert isinstance(pwallet["total_withdrawn"], dict)
    assert Decimal(pwallet["total_withdrawn"]["already_paid_out"]) == paid_net
    assert Decimal(pwallet["total_withdrawn"]["held_for_review"]) == pending_net + failed_net
    assert Decimal(pwallet["total_withdrawn"]["total"]) == paid_net + pending_net + failed_net


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
    assert len(ctx.captured_queries) <= 32


@pytest.mark.django_db
def test_user_earnings_ledger_page_two_continues_running_balance(system_config):
    _root, sponsor, buyer = _three_level_tree()
    order = _paid_order_for_buyer(buyer, "ORD-PAGED-LEDGER")
    for _ in range(25):
        CommissionLedger.objects.create(
            recipient=sponsor,
            source_user=buyer,
            order=order,
            commission_type=CommissionLedger.CommissionType.DIRECT,
            amount=Decimal("30.00"),
            net_amount=Decimal("30.00"),
            status=CommissionLedger.Status.CREDITED,
        )
    Wallet.objects.filter(user=sponsor).update(
        cash_balance=Decimal("750.00"),
        total_earned=Decimal("750.00"),
    )

    client = APIClient()
    client.force_authenticate(user=sponsor)
    page1 = client.get("/api/v1/user/earnings/?include=ledger&page=1&page_size=10")
    page2 = client.get("/api/v1/user/earnings/?include=ledger&page=2&page_size=10")

    assert page1.status_code == 200
    assert page2.status_code == 200
    page1_rows = page1.json()["data"]["ledger"]["rows"]
    page2_rows = page2.json()["data"]["ledger"]["rows"]
    expected_page2_start = Decimal(page1_rows[-1]["running_balance"]) - Decimal(page1_rows[-1]["net"])
    assert Decimal(page2_rows[0]["running_balance"]) == expected_page2_start


@pytest.mark.django_db
def test_user_earnings_ledger_includes_pending_withdrawal_row(system_config):
    _root, sponsor, buyer = _three_level_tree()
    CommissionEngine.process_order(_paid_order_for_buyer(buyer, "ORD-WD-1"))
    _prepare_sponsor_for_withdrawal(sponsor)

    client = APIClient()
    client.force_authenticate(user=sponsor)

    before = client.get("/api/v1/user/earnings/?include=ledger&page_size=5")
    assert before.status_code == 200
    rows_before = before.json()["data"]["ledger"]["rows"]
    commission_row = next(r for r in rows_before if r["kind"] == "COMMISSION")
    balance_before_withdraw = commission_row["running_balance"]

    wd = client.post(
        "/api/v1/user/wallet/withdraw/",
        {"band": 1, "amount": "200", "method": "UPI"},
        format="json",
    )
    assert wd.status_code == 200

    wallet = get_wallet_row(sponsor)
    after = client.get("/api/v1/user/earnings/?include=ledger&page_size=5")
    assert after.status_code == 200
    rows = after.json()["data"]["ledger"]["rows"]
    assert rows[0]["kind"] == "WITHDRAWAL"
    assert Decimal(rows[0]["gross"]) == Decimal("-200.00")
    assert Decimal(rows[0]["net"]) == Decimal("-200.00")
    assert rows[0]["status"] == WithdrawalRequest.Status.PENDING
    assert rows[0]["running_balance"] == str(wallet.cash_balance)

    commission_after = next(r for r in rows if r["kind"] == "COMMISSION")
    assert commission_after["running_balance"] == balance_before_withdraw


@pytest.mark.django_db
def test_user_earnings_ledger_includes_refund_row_when_rejected(system_config):
    _root, sponsor, buyer = _three_level_tree()
    CommissionEngine.process_order(_paid_order_for_buyer(buyer, "ORD-WD-2"))
    _prepare_sponsor_for_withdrawal(sponsor)

    member_client = APIClient()
    member_client.force_authenticate(user=sponsor)
    wd = member_client.post(
        "/api/v1/user/wallet/withdraw/",
        {"band": 1, "amount": "200", "method": "UPI"},
        format="json",
    )
    assert wd.status_code == 200
    wr_id = wd.json()["data"]["id"]

    admin_client = APIClient()
    admin_client.force_authenticate(user=_finance_admin())
    reject = admin_client.post(
        f"/api/v1/admin/withdrawals/{wr_id}/reject/",
        {"reason": "test reject"},
        format="json",
    )
    assert reject.status_code == 200

    wallet = get_wallet_row(sponsor)
    r = member_client.get("/api/v1/user/earnings/?include=ledger&page_size=10")
    rows = r.json()["data"]["ledger"]["rows"]
    assert rows[0]["kind"] == "WITHDRAWAL_REFUND"
    assert Decimal(rows[0]["gross"]) == Decimal("200.00")
    assert rows[1]["kind"] == "WITHDRAWAL"
    assert rows[1]["status"] == WithdrawalRequest.Status.REJECTED
    assert rows[0]["running_balance"] == str(wallet.cash_balance)


@pytest.mark.django_db
def test_user_earnings_ledger_type_filter_withdrawal(system_config):
    _root, sponsor, buyer = _three_level_tree()
    CommissionEngine.process_order(_paid_order_for_buyer(buyer, "ORD-WD-3"))
    _prepare_sponsor_for_withdrawal(sponsor)

    client = APIClient()
    client.force_authenticate(user=sponsor)
    client.post(
        "/api/v1/user/wallet/withdraw/",
        {"band": 1, "amount": "200", "method": "UPI"},
        format="json",
    )

    r = client.get("/api/v1/user/earnings/?include=ledger&type=withdrawal")
    assert r.status_code == 200
    ledger = r.json()["data"]["ledger"]
    assert ledger["total_count"] >= 1
    assert all(row["kind"] in ("WITHDRAWAL", "WITHDRAWAL_REFUND") for row in ledger["rows"])
    assert "withdrawal" in r.json()["data"]["filters"]["types"]


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
    sponsor.kyc_first_approved_at = timezone.now()
    sponsor.save(update_fields=["kyc_status", "kyc_first_approved_at"])
    MemberComplianceProfile.objects.create(user=sponsor)

    client = APIClient()
    client.force_authenticate(user=sponsor)
    with CaptureQueriesContext(connection) as ctx:
        client.get("/api/v1/user/payouts/?movements=true")
    assert len(ctx.captured_queries) <= 22


# ---------------------------------------------------------------------------
# Slot-band commission routing
# ---------------------------------------------------------------------------

def _seed_redeemed_slot(sponsor: User, *, count: int = 1) -> None:
    batch = SponsorSlotBatch.objects.create(
        issued_to=sponsor,
        band_number=2,
        total_codes=count,
        expires_at=timezone.now() + timezone.timedelta(days=30),
    )
    for i in range(count):
        SponsorSlotCode.objects.create(
            batch=batch,
            issued_to=sponsor,
            code=f"SP-RDM-{sponsor.pk}-{i}",
            status=SponsorSlotCode.Status.REDEEMED,
            expires_at=timezone.now() + timezone.timedelta(days=30),
        )


@pytest.mark.django_db
def test_slots_unit_follows_product_base_price(system_config):
    _root, sponsor, _buyer = _three_level_tree()
    _seed_redeemed_slot(sponsor, count=2)
    SystemConfig.objects.filter(pk=1).update(product_base_price=Decimal("241.72"))

    client = APIClient()
    client.force_authenticate(user=sponsor)
    r = client.get("/api/v1/user/earnings/?include=overview")
    assert r.status_code == 200
    slots = r.json()["data"]["summary"]["income"]["slots"]
    assert slots["redeemed"] == 2
    assert Decimal(slots["unit"]) == Decimal("241.72")
    assert Decimal(slots["amount"]) == Decimal("483.44")


@pytest.mark.django_db
def test_commission_in_slot_band_does_not_credit_cash(system_config):
    _root, sponsor, buyer = _three_level_tree()
    # Position sponsor inside band 2 (slot band).
    Wallet.objects.update_or_create(
        user=sponsor,
        defaults={
            "cash_balance": Decimal("0"),
            "total_earned": Decimal("4000"),
            "current_band": 2,
        },
    )

    CommissionEngine.process_order(_paid_order_for_buyer(buyer, "ORD-SLOT-BAND-1"))

    sponsor_wallet = get_wallet_row(sponsor)
    assert sponsor_wallet.cash_balance == Decimal("0"), (
        "slot-band credits must not bump cash_balance"
    )
    assert sponsor_wallet.total_earned == Decimal("4030"), (
        "slot-band credits must still bump total_earned"
    )
    direct_row = CommissionLedger.objects.get(
        recipient=sponsor,
        commission_type=CommissionLedger.CommissionType.DIRECT,
        order__order_number="ORD-SLOT-BAND-1",
    )
    assert direct_row.slot_band_held is True
    assert direct_row.status == CommissionLedger.Status.CREDITED
    # No WalletTransaction CREDIT row should have been emitted for the held credit.
    assert not WalletTransaction.objects.filter(
        user=sponsor,
        tx_type=WalletTransaction.TxType.CREDIT,
        reference="COMM-ORD-SLOT-BAND-1",
    ).exists()


@pytest.mark.django_db
def test_milestone_in_slot_band_does_not_credit_cash(system_config):
    _root, sponsor, buyer = _three_level_tree()
    Wallet.objects.update_or_create(
        user=sponsor,
        defaults={
            "cash_balance": Decimal("0"),
            "total_earned": Decimal("4000"),
            "current_band": 2,
        },
    )
    # Position sponsor at the milestone-1 trigger threshold (10 direct refs).
    # After the next order processes, direct_referral_count is bumped to 10
    # and `_maybe_milestone` fires.
    User.objects.filter(pk=sponsor.pk).update(direct_referral_count=9)
    sponsor.refresh_from_db()

    CommissionEngine.process_order(_paid_order_for_buyer(buyer, "ORD-SLOT-BAND-2"))

    sponsor_wallet = get_wallet_row(sponsor)
    assert sponsor_wallet.cash_balance == Decimal("0")
    ms = MilestoneRecord.objects.filter(user=sponsor).order_by("-id").first()
    assert ms is not None
    assert ms.status == "CREDITED"
    assert ms.slot_band_held is True
    assert not WalletTransaction.objects.filter(
        user=sponsor,
        tx_type=WalletTransaction.TxType.CREDIT,
        reference=f"MILESTONE-{ms.milestone_referrals}",
    ).exists()


@pytest.mark.django_db
def test_slot_band_held_rows_do_not_shift_running_balance(system_config):
    _root, sponsor, buyer = _three_level_tree()
    # Pre-existing CREDITED row in a cash band — bumps cash to 30.
    Wallet.objects.update_or_create(
        user=sponsor,
        defaults={
            "cash_balance": Decimal("30"),
            "total_earned": Decimal("30"),
            "current_band": 1,
        },
    )
    cash_row = CommissionLedger.objects.create(
        recipient=sponsor,
        source_user=buyer,
        order=_paid_order_for_buyer(buyer, "ORD-CASH-30"),
        commission_type=CommissionLedger.CommissionType.DIRECT,
        amount=Decimal("30"),
        net_amount=Decimal("30"),
        status=CommissionLedger.Status.CREDITED,
        slot_band_held=False,
    )
    # Later slot-band-held row.
    held_row = CommissionLedger.objects.create(
        recipient=sponsor,
        source_user=buyer,
        order=_paid_order_for_buyer(buyer, "ORD-HELD-30"),
        commission_type=CommissionLedger.CommissionType.DIRECT,
        amount=Decimal("30"),
        net_amount=Decimal("30"),
        status=CommissionLedger.Status.CREDITED,
        slot_band_held=True,
    )
    # Bump total_earned to reflect that the held credit funded a slot pool.
    Wallet.objects.filter(user=sponsor).update(total_earned=Decimal("4030"))

    client = APIClient()
    client.force_authenticate(user=sponsor)
    r = client.get("/api/v1/user/earnings/?include=ledger&page_size=10")
    assert r.status_code == 200
    rows = r.json()["data"]["ledger"]["rows"]
    held_serialized = next(row for row in rows if row["id"] == held_row.id)
    cash_serialized = next(row for row in rows if row["id"] == cash_row.id)
    # Held row reports the original net but no cash impact.
    assert held_serialized["slot_band_held"] is True
    assert held_serialized["cash_credited"] is False
    assert Decimal(held_serialized["net"]) == Decimal("30")
    # Both rows report the SAME running balance (current cash 30) because the
    # held row left cash flat between them.
    assert held_serialized["running_balance"] == cash_serialized["running_balance"]


@pytest.mark.django_db
def test_cash_crediting_resumes_after_band_exit(system_config):
    _root, sponsor, buyer = _three_level_tree()
    # Allow a second order from the same buyer to also generate commissions
    # so we can observe the band transition on the sponsor's wallet.
    SystemConfig.objects.filter(pk=1).update(is_repurchase_commission_allowed=True)
    # Position sponsor just below the band-2 ceiling (5000). One direct credit
    # of 30 lands at 4030 (held), then we manually bump and run another order
    # that crosses to band 3.
    Wallet.objects.update_or_create(
        user=sponsor,
        defaults={
            "cash_balance": Decimal("0"),
            "total_earned": Decimal("4970"),
            "current_band": 2,
        },
    )
    CommissionEngine.process_order(_paid_order_for_buyer(buyer, "ORD-EXIT-1"))
    held_row = CommissionLedger.objects.get(
        recipient=sponsor,
        order__order_number="ORD-EXIT-1",
        commission_type=CommissionLedger.CommissionType.DIRECT,
    )
    assert held_row.slot_band_held is True

    # Next order: total_earned is now 5000 → band 3 (cash band).
    sponsor_wallet = get_wallet_row(sponsor)
    assert sponsor_wallet.total_earned == Decimal("5000")
    CommissionEngine.process_order(_paid_order_for_buyer(buyer, "ORD-EXIT-2"))
    cash_row = CommissionLedger.objects.get(
        recipient=sponsor,
        order__order_number="ORD-EXIT-2",
        commission_type=CommissionLedger.CommissionType.DIRECT,
    )
    assert cash_row.slot_band_held is False, (
        "credits after the band ceiling must resume cash crediting"
    )
    sponsor_wallet.refresh_from_db()
    assert sponsor_wallet.cash_balance == Decimal("30")
    assert WalletTransaction.objects.filter(
        user=sponsor,
        tx_type=WalletTransaction.TxType.CREDIT,
        reference="COMM-ORD-EXIT-2",
    ).exists()


@pytest.mark.django_db
def test_reverse_slot_band_held_commission_only_reduces_total_earned(system_config):
    _root, sponsor, buyer = _three_level_tree()
    Wallet.objects.update_or_create(
        user=sponsor,
        defaults={
            "cash_balance": Decimal("100"),
            "total_earned": Decimal("4100"),
            "current_band": 2,
        },
    )
    held_order = _paid_order_for_buyer(buyer, "ORD-REV-HELD")
    CommissionLedger.objects.create(
        recipient=sponsor,
        source_user=buyer,
        order=held_order,
        commission_type=CommissionLedger.CommissionType.DIRECT,
        amount=Decimal("30"),
        net_amount=Decimal("30"),
        status=CommissionLedger.Status.CREDITED,
        slot_band_held=True,
    )
    CommissionEngine.reverse_commissions(held_order)
    sponsor_wallet = get_wallet_row(sponsor)
    assert sponsor_wallet.cash_balance == Decimal("100"), (
        "reversing a slot-band-held credit must not decrement cash_balance"
    )
    assert sponsor_wallet.total_earned == Decimal("4070")
    # No DEBIT row should have been emitted for the held reversal.
    assert not WalletTransaction.objects.filter(
        user=sponsor,
        tx_type=WalletTransaction.TxType.DEBIT,
        reference=f"REV-{held_order.order_number}",
    ).exists()


@pytest.mark.django_db
def test_user_earnings_ledger_hides_pre_kyc_held_rows(system_config):
    """Pre-KYC HELD/PENDING placeholder rows must not pollute the member's
    earnings ledger — they never funded the wallet and were just bookkeeping
    while admin approval was pending. Once an admin releases such a row
    (status flips to CREDITED) it must reappear so the line backing the
    wallet credit is visible."""
    _root, sponsor, buyer = _three_level_tree()

    # Pin sponsor's first-approval moment so we can place rows on either side
    # of it deterministically (the fixture set it to "now" already).
    approved_at = timezone.now()
    User.objects.filter(pk=sponsor.pk).update(kyc_first_approved_at=approved_at)
    sponsor.refresh_from_db()

    # Passive commission earned BEFORE KYC was approved: stays HELD with
    # net_amount=0 and never touches cash_balance. (auto_now_add already
    # fired, so we back-date the row via .update() to land before
    # `kyc_first_approved_at`.)
    pre_order = _paid_order_for_buyer(buyer, "ORD-PRE-KYC-PASSIVE")
    pre_held = CommissionLedger.objects.create(
        recipient=sponsor,
        source_user=buyer,
        order=pre_order,
        commission_type=CommissionLedger.CommissionType.UPLINE_L2,
        amount=Decimal("10"),
        net_amount=Decimal("0"),
        status=CommissionLedger.Status.HELD,
    )
    CommissionLedger.objects.filter(pk=pre_held.pk).update(
        created_at=approved_at - timezone.timedelta(days=1)
    )

    # Real credit earned AFTER KYC approval — this is what the member
    # should see in the ledger.
    post_order = _paid_order_for_buyer(buyer, "ORD-POST-KYC-DIRECT")
    post_credit = CommissionLedger.objects.create(
        recipient=sponsor,
        source_user=buyer,
        order=post_order,
        commission_type=CommissionLedger.CommissionType.DIRECT,
        amount=Decimal("30"),
        net_amount=Decimal("30"),
        status=CommissionLedger.Status.CREDITED,
    )
    Wallet.objects.filter(user=sponsor).update(
        cash_balance=Decimal("30"),
        total_earned=Decimal("30"),
    )

    client = APIClient()
    client.force_authenticate(user=sponsor)

    r = client.get("/api/v1/user/earnings/?include=overview,ledger&page=1&page_size=20")
    assert r.status_code == 200
    rows = r.json()["data"]["ledger"]["rows"]
    ids = [row["id"] for row in rows]
    assert post_credit.id in ids
    assert pre_held.id not in ids, (
        "pre-KYC HELD passive credits must not show up in the member ledger"
    )

    # Also confirm the dedicated passive filter excludes the pre-KYC HELD
    # row (it had status=HELD anyway, but the date guard reinforces this).
    rp = client.get("/api/v1/user/earnings/?include=ledger&type=passive")
    assert rp.status_code == 200
    assert pre_held.id not in [row["id"] for row in rp.json()["data"]["ledger"]["rows"]]

    # Admin releases the pre-KYC HELD row: status flips to CREDITED and
    # net_amount becomes non-zero. created_at stays pre-KYC (the release
    # service does not touch it) — the status escape clause in the SQL
    # must let the row resurface so the user can see the line that
    # corresponds to the new wallet credit.
    CommissionLedger.objects.filter(pk=pre_held.pk).update(
        status=CommissionLedger.Status.CREDITED,
        amount=Decimal("10"),
        net_amount=Decimal("10"),
    )

    r2 = client.get("/api/v1/user/earnings/?include=ledger&page=1&page_size=20")
    assert r2.status_code == 200
    ids2 = [row["id"] for row in r2.json()["data"]["ledger"]["rows"]]
    assert pre_held.id in ids2, (
        "an admin-released pre-KYC commission must reappear once it's credited"
    )
