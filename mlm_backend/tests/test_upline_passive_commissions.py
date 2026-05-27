from __future__ import annotations

from decimal import Decimal
from typing import Tuple

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.admin_panel.models import SystemConfig
from apps.agreements.models import MemberComplianceProfile
from apps.commissions.engine import CommissionEngine
from apps.commissions.models import CommissionLedger
from apps.mlm_tree.services import BinaryTreeService
from apps.payments.models import Order
from apps.users.models import User
from apps.users.services import allocate_member_identity


def _create_linear_binary_chain(depth: int) -> Tuple[list[User], User]:
    """
    Create a linear binary chain of `depth` users:
      users[0] (top) -> users[1] -> ... -> users[depth-1] (bottom buyer)

    Sponsor field is set to the binary parent for each node below the top.
    Returns (users, bottom).
    """

    assert depth >= 2
    users: list[User] = []

    # Top node (no sponsor).
    mid, ref, link = allocate_member_identity()
    top = User(
        phone="+919000000001",
        full_name="Top",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        pan_number="ABCDE1234F",
        kyc_status=User.KYCStatus.VERIFIED,
    )
    top.set_unusable_password()
    top.save()
    top.is_member = True
    top.save()
    BinaryTreeService.place_member(top, None)
    MemberComplianceProfile.objects.get_or_create(user=top)
    if not top.kyc_first_approved_at:
        top.kyc_first_approved_at = timezone.now()
        top.save(update_fields=["kyc_first_approved_at"])
    users.append(top)

    parent = top
    for i in range(1, depth):
        mid_i, ref_i, link_i = allocate_member_identity()
        u = User(
            phone=f"+91900000000{i+1}",
            full_name=f"U{i}",
            member_id=mid_i,
            referral_code=ref_i,
            referral_link=link_i,
            sponsor=parent,
            pan_number="ABCDE1234F",
            kyc_status=User.KYCStatus.VERIFIED,
        )
        u.set_unusable_password()
        u.save()
        u.is_member = True
        u.save()
        BinaryTreeService.place_member(u, parent)
        MemberComplianceProfile.objects.get_or_create(user=u)
        if not u.kyc_first_approved_at:
            u.kyc_first_approved_at = timezone.now()
            u.save(update_fields=["kyc_first_approved_at"])
        users.append(u)
        parent = u

    return users, users[-1]


def _paid_order_for_buyer(*, buyer: User, order_number: str) -> Order:
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
    )


@pytest.mark.django_db
def test_passive_3_levels_excluding_sponsor_when_sponsor_is_binary_parent(system_config: SystemConfig):
    """
    Required rule:
    - DIRECT always goes to buyer.sponsor
    - Passive credits go to the first 3 binary uplines, excluding the sponsor
    """

    # Chain: top -> u1 -> u2 -> sponsor(u3) -> buyer(u4)
    users, buyer = _create_linear_binary_chain(depth=5)
    top, u1, u2, sponsor, _buyer = users[0], users[1], users[2], users[3], users[4]
    assert buyer == _buyer

    order = _paid_order_for_buyer(buyer=buyer, order_number="ORD-PASSIVE-EXCLUDE-SPONSOR")
    CommissionEngine.process_order(order)

    assert CommissionLedger.objects.filter(
        order=order,
        recipient=sponsor,
        source_user=buyer,
        commission_type=CommissionLedger.CommissionType.DIRECT,
        status=CommissionLedger.Status.CREDITED,
    ).exists()

    # Expected passive recipients (excluding sponsor) in order:
    # UPLINE_L1 -> u2, UPLINE_L2 -> u1, UPLINE_L3 -> top
    expected = [
        (u2, CommissionLedger.CommissionType.UPLINE_L1),
        (u1, CommissionLedger.CommissionType.UPLINE_L2),
        (top, CommissionLedger.CommissionType.UPLINE_L3),
    ]
    for recipient, ctype in expected:
        rows = CommissionLedger.objects.filter(
            order=order,
            recipient=recipient,
            source_user=buyer,
            commission_type=ctype,
            status=CommissionLedger.Status.CREDITED,
        )
        assert rows.exists(), f"missing passive {ctype} for recipient={recipient.member_id}"
        assert rows.count() == 1
        assert rows.first().net_amount == Decimal("10")

    # Sponsor must not receive passive credits from its own join.
    assert not CommissionLedger.objects.filter(
        order=order,
        recipient=sponsor,
        source_user=buyer,
        commission_type__in=(
            CommissionLedger.CommissionType.UPLINE_L1,
            CommissionLedger.CommissionType.UPLINE_L2,
            CommissionLedger.CommissionType.UPLINE_L3,
        ),
    ).exists()


@pytest.mark.django_db
def test_passive_credits_when_sponsor_is_outside_binary_chain(system_config: SystemConfig):
    """
    If buyer.sponsor is NOT on the buyer's binary parent chain, passive credits
    should go to the binary uplines (immediate parent, grandparent, great-grandparent).
    """

    users, buyer = _create_linear_binary_chain(depth=5)
    top, u1, u2, binary_parent, _buyer = users[0], users[1], users[2], users[3], users[4]
    assert buyer == _buyer

    # Create a sponsor that is NOT on the binary ancestor chain.
    mid, ref, link = allocate_member_identity()
    sponsor_other = User(
        phone="+919111111111",
        full_name="OtherSponsor",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        pan_number="ABCDE1234F",
        kyc_status=User.KYCStatus.VERIFIED,
    )
    sponsor_other.set_unusable_password()
    sponsor_other.save()
    sponsor_other.is_member = True
    sponsor_other.save()
    MemberComplianceProfile.objects.get_or_create(user=sponsor_other)
    if not sponsor_other.kyc_first_approved_at:
        sponsor_other.kyc_first_approved_at = timezone.now()
        sponsor_other.save(update_fields=["kyc_first_approved_at"])

    # Point buyer's direct sponsor to the outside account.
    buyer.sponsor = sponsor_other
    buyer.save(update_fields=["sponsor"])

    order = _paid_order_for_buyer(buyer=buyer, order_number="ORD-PASSIVE-SPONSOR-OUTSIDE")
    CommissionEngine.process_order(order)

    # Direct goes to sponsor_other.
    assert CommissionLedger.objects.filter(
        order=order,
        recipient=sponsor_other,
        source_user=buyer,
        commission_type=CommissionLedger.CommissionType.DIRECT,
        status=CommissionLedger.Status.CREDITED,
    ).exists()

    # Passive should go to binary uplines: binary_parent, u2, u1.
    expected = [
        (binary_parent, CommissionLedger.CommissionType.UPLINE_L1),
        (u2, CommissionLedger.CommissionType.UPLINE_L2),
        (u1, CommissionLedger.CommissionType.UPLINE_L3),
    ]
    for recipient, ctype in expected:
        rows = CommissionLedger.objects.filter(
            order=order,
            recipient=recipient,
            source_user=buyer,
            commission_type=ctype,
            status=CommissionLedger.Status.CREDITED,
        )
        assert rows.exists(), f"missing passive {ctype} for recipient={recipient.member_id}"
        assert rows.count() == 1
        assert rows.first().net_amount == Decimal("10")

    # Sponsor_other must not receive passive credits.
    assert not CommissionLedger.objects.filter(
        order=order,
        recipient=sponsor_other,
        source_user=buyer,
        commission_type__in=(
            CommissionLedger.CommissionType.UPLINE_L1,
            CommissionLedger.CommissionType.UPLINE_L2,
            CommissionLedger.CommissionType.UPLINE_L3,
        ),
    ).exists()


@pytest.mark.django_db
def test_reconcile_upline_passive_commissions_backfills_missing_third_credit_and_is_idempotent(system_config: SystemConfig):
    """
    Validate the post-deploy reconciliation command:
    - relabel/pay missing passive credits
    - safe to run twice (idempotent)
    """

    users, buyer = _create_linear_binary_chain(depth=5)
    top, u1, u2, sponsor, _buyer = users[0], users[1], users[2], users[3], users[4]
    assert buyer == _buyer

    order = _paid_order_for_buyer(buyer=buyer, order_number="ORD-RECONCILE-PASSIVE")

    # Simulate historic state where the 1st and 2nd passive credits exist,
    # but the 3rd (UPLINE_L3 -> top) is missing.
    cfg = SystemConfig.objects.get(pk=1)
    cap = cfg.earning_cap
    CommissionEngine._credit_user(
        recipient=sponsor,
        source=buyer,
        order=order,
        ctype=CommissionLedger.CommissionType.DIRECT,
        gross=cfg.direct_commission,
        cap=cap,
    )
    CommissionEngine._credit_user(
        recipient=u2,
        source=buyer,
        order=order,
        ctype=CommissionLedger.CommissionType.UPLINE_L1,
        gross=cfg.upline_commission,
        cap=cap,
    )
    CommissionEngine._credit_user(
        recipient=u1,
        source=buyer,
        order=order,
        ctype=CommissionLedger.CommissionType.UPLINE_L2,
        gross=cfg.upline_commission,
        cap=cap,
    )
    assert not CommissionLedger.objects.filter(
        order=order,
        recipient=top,
        source_user=buyer,
        commission_type=CommissionLedger.CommissionType.UPLINE_L3,
        status__in=(CommissionLedger.Status.CREDITED, CommissionLedger.Status.HELD),
    ).exists()

    call_command(
        "reconcile_upline_passive_commissions",
        "--apply",
        "--order-id",
        order.id,
    )

    assert CommissionLedger.objects.filter(
        order=order,
        recipient=top,
        source_user=buyer,
        commission_type=CommissionLedger.CommissionType.UPLINE_L3,
        status=CommissionLedger.Status.CREDITED,
    ).exists()
    assert CommissionLedger.objects.filter(
        order=order,
        recipient=top,
        source_user=buyer,
        commission_type=CommissionLedger.CommissionType.UPLINE_L3,
        status__in=(CommissionLedger.Status.CREDITED, CommissionLedger.Status.HELD),
    ).count() == 1

    # Idempotency: second apply must not create duplicates.
    call_command(
        "reconcile_upline_passive_commissions",
        "--apply",
        "--order-id",
        order.id,
    )
    assert CommissionLedger.objects.filter(
        order=order,
        recipient=top,
        source_user=buyer,
        commission_type=CommissionLedger.CommissionType.UPLINE_L3,
        status__in=(CommissionLedger.Status.CREDITED, CommissionLedger.Status.HELD),
    ).count() == 1

