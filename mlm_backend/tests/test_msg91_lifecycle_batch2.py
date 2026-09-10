"""MSG91 lifecycle campaigns batch 2: referral, milestone, cap, withdrawal, refund."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.admin_panel.utils import get_system_config
from apps.commissions.engine import CommissionEngine
from apps.commissions.models import CommissionLedger
from apps.commissions.milestone_notify import schedule_milestone_achieved_notification
from apps.notifications.models import NotificationLog
from apps.payments.models import Order, RefundRequest
from apps.users.models import User
from apps.users.services import allocate_member_identity
from apps.wallet.models import Wallet, WithdrawalRequest
from tests.test_earnings_bundles import (
    _finance_admin,
    _paid_order_for_buyer,
    _prepare_sponsor_for_withdrawal,
    _three_level_tree,
)


def _enable_msg91(*, development_mode: bool = False, authkey: str = "test-msg91-key"):
    cfg = get_system_config()
    cfg.development_mode = development_mode
    cfg.msg91_authkey = authkey
    cfg.save(update_fields=["development_mode", "msg91_authkey"])


@pytest.mark.django_db(transaction=True)
@patch("apps.notifications.msg91.post_campaign", return_value=True)
def test_referral_joined_on_first_qualifying_purchase(mock_post, system_config):
    _enable_msg91()
    _root, sponsor, buyer = _three_level_tree()
    CommissionEngine.process_order(_paid_order_for_buyer(buyer, "ORD-REF-JOIN"))

    slugs = [call.args[0] for call in mock_post.call_args_list]
    assert "referral-joined" in slugs
    assert NotificationLog.objects.filter(
        user=sponsor, template_key="referral_joined", channel="MSG91"
    ).exists()


@pytest.mark.django_db(transaction=True)
@patch("apps.notifications.msg91.post_campaign", return_value=True)
def test_milestone_achieved_on_threshold(mock_post, system_config):
    _enable_msg91()
    cfg = get_system_config()
    cfg.auto_process_milestone_bonuses = False
    cfg.save(update_fields=["auto_process_milestone_bonuses"])

    mid, ref, link = allocate_member_identity()
    sponsor = User(
        phone="+919900000101",
        email="milestone-sponsor@test.dev",
        full_name="Milestone Sponsor",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
    )
    sponsor.set_unusable_password()
    sponsor.save()
    sponsor.direct_referral_count = 10
    sponsor.save(update_fields=["direct_referral_count"])

    CommissionEngine._maybe_milestone(sponsor, cfg)

    slugs = [call.args[0] for call in mock_post.call_args_list]
    assert "milestone-achieved" in slugs
    rejected = [
        call
        for call in mock_post.call_args_list
        if call.args[0] == "milestone-achieved"
    ]
    body = rejected[0].args[1]
    variables = body["data"]["sendTo"][0]["variables"]
    assert variables["milestone_achieved_3:reward_amount"]["value"] == "300.00"
    assert variables["milestone_achieved_utility:body_reward_amount"]["value"] == "300.00"
    assert "body_2" not in variables
    assert NotificationLog.objects.filter(
        user=sponsor, template_key="milestone_achieved", channel="MSG91"
    ).exists()


@pytest.mark.django_db(transaction=True)
@patch("apps.notifications.msg91.post_campaign", return_value=True)
def test_earning_cap_achieved_once(mock_post, system_config):
    _enable_msg91()
    cfg = get_system_config()
    cap = cfg.earning_cap

    mid, ref, link = allocate_member_identity()
    recipient = User(
        phone="+919900000102",
        email="cap-user@test.dev",
        full_name="Cap User",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        kyc_status=User.KYCStatus.VERIFIED,
    )
    recipient.set_unusable_password()
    recipient.save()
    recipient.kyc_first_approved_at = timezone.now()
    recipient.save(update_fields=["kyc_first_approved_at"])

    mid_b, ref_b, link_b = allocate_member_identity()
    buyer = User(
        phone="+919900000103",
        full_name="Buyer",
        member_id=mid_b,
        referral_code=ref_b,
        referral_link=link_b,
        sponsor=recipient,
    )
    buyer.set_unusable_password()
    buyer.save()

    Wallet.objects.create(user=recipient, total_earned=cap - Decimal("30"), cash_balance=Decimal("0"))
    order = Order.objects.create(
        user=buyer,
        order_number="ORD-CAP-MSG91",
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("241.72"),
        is_retail_purchase=False,
        status=Order.Status.PAID,
        paid_at=timezone.now(),
    )
    CommissionEngine._credit_user(
        recipient=recipient,
        source=buyer,
        order=order,
        ctype=CommissionLedger.CommissionType.DIRECT,
        gross=Decimal("30"),
        cap=cap,
    )

    recipient.refresh_from_db()
    assert recipient.account_status == User.AccountStatus.CAPPED
    slugs = [call.args[0] for call in mock_post.call_args_list]
    assert slugs.count("earning-cap-achieved") == 1
    assert NotificationLog.objects.filter(
        user=recipient, template_key="earning_cap_achieved", channel="MSG91"
    ).exists()


@pytest.mark.django_db(transaction=True)
@patch("apps.notifications.msg91.post_campaign", return_value=True)
def test_withdrawal_submitted_and_approved(mock_post, system_config):
    _enable_msg91()
    _root, sponsor, buyer = _three_level_tree()
    CommissionEngine.process_order(_paid_order_for_buyer(buyer, "ORD-WD-SUB"))
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

    slugs = [call.args[0] for call in mock_post.call_args_list]
    assert "withdrawal-submitted" in slugs

    admin_client = APIClient()
    admin_client.force_authenticate(user=_finance_admin())
    approve = admin_client.post(f"/api/v1/admin/withdrawals/{wr_id}/approve/", {}, format="json")
    assert approve.status_code == 200

    slugs = [call.args[0] for call in mock_post.call_args_list]
    assert "withdrawal-approved" in slugs
    assert NotificationLog.objects.filter(
        user=sponsor, template_key="withdrawal_approved", channel="MSG91"
    ).exists()


@pytest.mark.django_db(transaction=True)
@patch("apps.notifications.msg91.post_campaign", return_value=True)
def test_withdrawal_rejected(mock_post, system_config):
    _enable_msg91()
    _root, sponsor, buyer = _three_level_tree()
    CommissionEngine.process_order(_paid_order_for_buyer(buyer, "ORD-WD-REJ"))
    _prepare_sponsor_for_withdrawal(sponsor)

    member_client = APIClient()
    member_client.force_authenticate(user=sponsor)
    wd = member_client.post(
        "/api/v1/user/wallet/withdraw/",
        {"band": 1, "amount": "200", "method": "UPI"},
        format="json",
    )
    wr_id = wd.json()["data"]["id"]

    admin_client = APIClient()
    admin_client.force_authenticate(user=_finance_admin())
    reject = admin_client.post(
        f"/api/v1/admin/withdrawals/{wr_id}/reject/",
        {"reason": "name mismatch"},
        format="json",
    )
    assert reject.status_code == 200

    slugs = [call.args[0] for call in mock_post.call_args_list]
    assert "withdraw-rejected" in slugs
    rejected_calls = [c for c in mock_post.call_args_list if c.args[0] == "withdraw-rejected"]
    variables = rejected_calls[0].args[1]["data"]["sendTo"][0]["variables"]
    assert variables["withdrawal_rejected_2:rejection_reason"]["value"] == "name mismatch"
    assert variables["withdrawal_rejected:body_rejection_reason"]["value"] == "name mismatch"


@pytest.mark.django_db(transaction=True)
@patch("apps.notifications.msg91.post_campaign", return_value=True)
def test_refund_rejected(mock_post, system_config):
    _enable_msg91()
    mid, ref, link = allocate_member_identity()
    buyer = User(
        phone="+919900000104",
        email="refund-buyer@test.dev",
        full_name="Refund Buyer",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
    )
    buyer.set_unusable_password()
    buyer.save()
    order = Order.objects.create(
        user=buyer,
        order_number="ORD-REF-REJ",
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("241.72"),
        status=Order.Status.PAID,
        paid_at=timezone.now(),
    )
    rr = RefundRequest.objects.create(
        reference="RET-MSG91-REJ",
        order=order,
        user=buyer,
        amount=Decimal("241.72"),
    )

    admin_client = APIClient()
    admin_client.force_authenticate(user=_finance_admin())
    reject = admin_client.post(
        f"/api/v1/admin/refunds/{rr.id}/reject/",
        {"reason": "outside refund window"},
        format="json",
    )
    assert reject.status_code == 200

    slugs = [call.args[0] for call in mock_post.call_args_list]
    assert "refund-rejected" in slugs
    assert NotificationLog.objects.filter(
        user=buyer, template_key="refund_rejected", channel="MSG91"
    ).exists()


@pytest.mark.django_db
@patch("apps.notifications.msg91.post_campaign", return_value=True)
def test_refund_approved_task(mock_post, system_config):
    _enable_msg91()
    from apps.notifications.tasks import send_refund_approved_task

    mid, ref, link = allocate_member_identity()
    buyer = User(
        phone="+919900000105",
        email="refund-ok@test.dev",
        full_name="Refund OK",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
    )
    buyer.set_unusable_password()
    buyer.save()
    order = Order.objects.create(
        user=buyer,
        order_number="ORD-REF-OK",
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("241.72"),
        status=Order.Status.PAID,
        paid_at=timezone.now(),
    )
    rr = RefundRequest.objects.create(
        reference="RET-MSG91-OK",
        order=order,
        user=buyer,
        amount=Decimal("300"),
        status=RefundRequest.Status.APPROVED,
    )

    assert send_refund_approved_task(rr.pk) is True
    mock_post.assert_called_once()
    assert mock_post.call_args[0][0] == "refund-approved"
    variables = mock_post.call_args[0][1]["data"]["sendTo"][0]["variables"]
    assert variables["refund_approved_2:refund_amount"]["value"] == "300.00"
    assert variables["refund_approved:body_refund_amount"]["value"] == "300.00"


@pytest.mark.django_db
@patch("apps.notifications.msg91.post_campaign", return_value=True)
def test_batch2_skipped_in_development_mode(mock_post, system_config):
    _enable_msg91(development_mode=True)
    from apps.notifications.tasks import send_referral_joined_task

    mid, ref, link = allocate_member_identity()
    sponsor = User(
        phone="+919900000106",
        full_name="Sponsor",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
    )
    sponsor.set_unusable_password()
    sponsor.save()
    mid2, ref2, link2 = allocate_member_identity()
    referral = User(
        phone="+919900000107",
        full_name="Referral",
        member_id=mid2,
        referral_code=ref2,
        referral_link=link2,
    )
    referral.set_unusable_password()
    referral.save()

    assert send_referral_joined_task(sponsor.pk, referral.pk) is False
    mock_post.assert_not_called()

    schedule_milestone_achieved_notification(
        user=sponsor,
        threshold=10,
        bonus_amount=Decimal("300"),
    )
    from apps.notifications.tasks import send_milestone_achieved_task

    assert (
        send_milestone_achieved_task(sponsor.pk, 10, "300.00", "T1 Milestone") is False
    )
    mock_post.assert_not_called()
