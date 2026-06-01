"""Sticky KYC: commission credit vs withdrawal gate."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APIClient

from apps.admin_panel.utils import get_system_config
from apps.agreements.models import MemberComplianceProfile
from apps.commissions.engine import CommissionEngine
from apps.commissions.models import CommissionLedger
from apps.payments.models import Order
from apps.users.models import User
from apps.users.services import allocate_member_identity
from apps.wallet.models import Wallet
from tests.conftest import unique_test_aadhaar, unique_test_pan


def _member(phone: str, **kwargs) -> User:
    mid, ref, link = allocate_member_identity()
    u = User(
        phone=phone,
        full_name=kwargs.get("full_name", "Member"),
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        kyc_status=kwargs.get("kyc_status", User.KYCStatus.PENDING),
        pan_number=unique_test_pan(),
    )
    u.set_unusable_password()
    u.save()
    if kwargs.get("kyc_first_approved_at"):
        u.kyc_first_approved_at = kwargs["kyc_first_approved_at"]
        u.save(update_fields=["kyc_first_approved_at"])
    return u


def _paid_order(user: User, order_number: str) -> Order:
    now = timezone.now()
    return Order.objects.create(
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


def _support_admin() -> User:
    mid, ref, link = allocate_member_identity()
    admin = User(
        phone="+919900009900",
        full_name="Support Admin",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        is_staff=True,
        role=User.Role.SUPPORT,
    )
    admin.set_unusable_password()
    admin.save()
    return admin


def _profile_with_min_docs(user: User) -> MemberComplianceProfile:
    profile = MemberComplianceProfile.objects.create(user=user)
    profile.pan_number = unique_test_pan()
    profile.aadhar_number = unique_test_aadhaar()
    profile.pan_document = SimpleUploadedFile("pan.pdf", b"fake", content_type="application/pdf")
    profile.aadhar_front = SimpleUploadedFile(
        "aad_front.pdf", b"fake", content_type="application/pdf"
    )
    profile.aadhar_back = SimpleUploadedFile(
        "aad_back.pdf", b"fake", content_type="application/pdf"
    )
    profile.save()
    return profile


@pytest.mark.django_db
def test_never_approved_user_commission_is_held(system_config):
    """Brand-new user without kyc_first_approved_at still gets HELD commissions."""
    recipient = _member("+919110110001")
    buyer = _member("+919110110002", sponsor=recipient)
    order = _paid_order(buyer, "ORD-STICKY-NEVER")
    cfg = get_system_config()

    CommissionEngine._credit_user(
        recipient=recipient,
        source=buyer,
        order=order,
        ctype=CommissionLedger.CommissionType.DIRECT,
        gross=cfg.direct_commission,
        cap=cfg.earning_cap,
    )

    row = CommissionLedger.objects.get(recipient=recipient, order=order)
    assert row.status == CommissionLedger.Status.HELD
    assert row.net_amount == Decimal("0")
    assert not Wallet.objects.filter(user=recipient).exists()


@pytest.mark.django_db
def test_previously_approved_user_credits_while_kyc_pending(system_config):
    """User with kyc_first_approved_at earns normally even when kyc_status is PENDING."""
    approved_at = timezone.now() - timedelta(days=30)
    recipient = _member(
        "+919120120001",
        kyc_status=User.KYCStatus.PENDING,
        kyc_first_approved_at=approved_at,
    )
    buyer = _member("+919120120002", sponsor=recipient)
    order = _paid_order(buyer, "ORD-STICKY-PREV")
    cfg = get_system_config()

    CommissionEngine._credit_user(
        recipient=recipient,
        source=buyer,
        order=order,
        ctype=CommissionLedger.CommissionType.DIRECT,
        gross=cfg.direct_commission,
        cap=cfg.earning_cap,
    )

    row = CommissionLedger.objects.get(recipient=recipient, order=order)
    assert row.status == CommissionLedger.Status.CREDITED
    assert row.net_amount > 0
    wallet = Wallet.objects.get(user=recipient)
    assert wallet.total_earned > 0


@pytest.mark.django_db
def test_withdrawal_blocked_when_kyc_pending_even_if_previously_approved(system_config):
    """Withdrawal uses current kyc_status; sticky approval does not bypass payout gate."""
    approved_at = timezone.now() - timedelta(days=30)
    user = _member(
        "+919130130001",
        kyc_status=User.KYCStatus.PENDING,
        kyc_first_approved_at=approved_at,
    )
    user.upi_id = "member@upi"
    user.payout_preference = User.PayoutPreference.UPI
    user.save(update_fields=["upi_id", "payout_preference"])
    Wallet.objects.create(
        user=user,
        cash_balance=Decimal("500.00"),
        total_earned=Decimal("500.00"),
        current_band=1,
    )

    client = APIClient()
    client.force_authenticate(user=user)
    r = client.post(
        "/api/v1/user/wallet/withdraw/",
        {"band": 1, "amount": "200", "method": "UPI"},
        format="json",
    )
    assert r.status_code == 403
    body = r.json()
    assert body["success"] is False
    assert "kyc" in (body.get("message") or "").lower()


@pytest.mark.django_db
def test_first_time_approval_does_not_release_pre_approval_held(
    system_config, django_capture_on_commit_callbacks
):
    """First-time KYC approval must NOT retroactively credit commissions earned while never approved."""
    admin = _support_admin()
    earner = _member("+919140140001")
    assert earner.kyc_first_approved_at is None
    buyer = _member("+919140140002", sponsor=earner)
    order = _paid_order(buyer, "ORD-STICKY-FIRST")
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
    _profile_with_min_docs(earner)

    client = APIClient()
    client.force_authenticate(user=admin)
    with django_capture_on_commit_callbacks(execute=True):
        r = client.post(
            f"/api/v1/admin/users/{earner.id}/compliance/approve/",
            {},
            format="json",
        )
    assert r.status_code == 200, r.content

    earner.refresh_from_db()
    assert earner.kyc_status == User.KYCStatus.VERIFIED
    assert earner.kyc_first_approved_at is not None

    row = CommissionLedger.objects.get(recipient=earner, order=order)
    assert row.status == CommissionLedger.Status.HELD, (
        "Pre-approval HELD must be forfeited on first-time approval, not retroactively credited"
    )
    assert row.net_amount == Decimal("0")
    wallet = Wallet.objects.get(user=earner)
    assert wallet.total_earned == Decimal("0")


@pytest.mark.django_db
def test_re_approval_releases_post_first_approval_held(
    system_config, django_capture_on_commit_callbacks
):
    """Re-approval (user was approved before) must auto-release HELD rows created after the first approval."""
    admin = _support_admin()
    earlier = timezone.now() - timedelta(days=30)
    earner = _member(
        "+919150150001",
        kyc_status=User.KYCStatus.PENDING,
        kyc_first_approved_at=earlier,
    )
    buyer = _member("+919150150002", sponsor=earner)
    order = _paid_order(buyer, "ORD-STICKY-REAPP")
    # HELD row created NOW, well after the user's first approval.
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
    _profile_with_min_docs(earner)

    client = APIClient()
    client.force_authenticate(user=admin)
    with django_capture_on_commit_callbacks(execute=True):
        r = client.post(
            f"/api/v1/admin/users/{earner.id}/compliance/approve/",
            {},
            format="json",
        )
    assert r.status_code == 200, r.content

    row = CommissionLedger.objects.get(recipient=earner, order=order)
    assert row.status == CommissionLedger.Status.CREDITED
    assert row.net_amount > 0
    wallet = Wallet.objects.get(user=earner)
    assert wallet.total_earned == row.amount


@pytest.mark.django_db
def test_re_approval_does_not_release_pre_first_approval_held(
    system_config, django_capture_on_commit_callbacks
):
    """Even on re-approval, rows created before the user's first approval must stay HELD."""
    admin = _support_admin()
    first_approved_at = timezone.now()
    earner = _member(
        "+919160160001",
        kyc_status=User.KYCStatus.PENDING,
        kyc_first_approved_at=first_approved_at,
    )
    buyer = _member("+919160160002", sponsor=earner)
    order = _paid_order(buyer, "ORD-STICKY-PRE")
    old = CommissionLedger.objects.create(
        recipient=earner,
        source_user=buyer,
        order=order,
        commission_type=CommissionLedger.CommissionType.DIRECT,
        amount=Decimal("30.00"),
        tds_deducted=Decimal("0"),
        net_amount=Decimal("0"),
        status=CommissionLedger.Status.HELD,
    )
    pre_first = first_approved_at - timedelta(days=10)
    CommissionLedger.objects.filter(pk=old.pk).update(created_at=pre_first)
    Wallet.objects.create(user=earner, cash_balance=Decimal("0"), total_earned=Decimal("0"))
    _profile_with_min_docs(earner)

    client = APIClient()
    client.force_authenticate(user=admin)
    with django_capture_on_commit_callbacks(execute=True):
        r = client.post(
            f"/api/v1/admin/users/{earner.id}/compliance/approve/",
            {},
            format="json",
        )
    assert r.status_code == 200, r.content

    row = CommissionLedger.objects.get(pk=old.pk)
    assert row.status == CommissionLedger.Status.HELD
    assert row.net_amount == Decimal("0")
    wallet = Wallet.objects.get(user=earner)
    assert wallet.total_earned == Decimal("0")
