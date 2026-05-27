"""Earning-cap (CAPPED) account lockdown: referrals, slots, placements, /auth/me."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.agreements.models import MemberComplianceProfile
from apps.mlm_tree import placement as placement_mod
from apps.mlm_tree.models import BinaryNode
from apps.mlm_tree.services import BinaryTreeService
from apps.mlm_tree.tasks import auto_place_pending_placements
from apps.payments.models import Order
from apps.sponsor_slots.models import SponsorSlotBatch, SponsorSlotCode
from apps.users.models import User
from apps.users.services import allocate_member_identity, company_fallback_sponsor
from apps.wallet.models import Wallet


@pytest.fixture
def fake_razorpay_client(monkeypatch):
    class OrderApi:
        @staticmethod
        def create(payload):
            return {"id": "rzord_fake_cap", "amount": payload["amount"]}

    class Client:
        order = OrderApi()

    monkeypatch.setattr("apps.payments.services._client", lambda: Client())


def _company_admin() -> User:
    return User.objects.create_superuser(
        "cap-lock-admin@test.dev",
        "pw",
        full_name="Platform Admin",
        email="cap-lock-admin@test.dev",
    )


def _member(
    phone: str,
    *,
    sponsor: User | None = None,
    account_status: str = User.AccountStatus.ACTIVE,
    kyc_verified: bool = False,
) -> User:
    mid, ref, link = allocate_member_identity()
    u = User(
        phone=phone,
        full_name="Cap Member",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        sponsor=sponsor,
        account_status=account_status,
        is_member=True,
    )
    if kyc_verified:
        u.kyc_status = User.KYCStatus.VERIFIED
    u.set_unusable_password()
    u.save()
    if kyc_verified:
        MemberComplianceProfile.objects.get_or_create(user=u)
    return u


def _paid_order(user: User, **kwargs) -> Order:
    suffix = kwargs.pop("suffix", "x")
    now = timezone.now()
    defaults = dict(
        user=user,
        order_number=f"ORD-CAP-{user.id}-{suffix}",
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


def _slot_code(issuer: User, code: str = "CAPSLOT01") -> SponsorSlotCode:
    batch = SponsorSlotBatch.objects.create(
        issued_to=issuer,
        band_number=1,
        expires_at=timezone.now() + timedelta(days=30),
    )
    return SponsorSlotCode.objects.create(
        batch=batch,
        issued_to=issuer,
        code=code,
        status=SponsorSlotCode.Status.ACTIVE,
        expires_at=batch.expires_at,
    )


@pytest.mark.django_db
def test_register_otp_rejects_capped_sponsor_referral(system_config):
    _company_admin()
    capped = _member("+918100000001", account_status=User.AccountStatus.CAPPED)
    client = APIClient()
    r = client.post(
        "/api/v1/auth/register/send-otp/",
        {
            "phone": "+918100000099",
            "full_name": "New User",
            "referral_code": capped.referral_code,
        },
        format="json",
    )
    assert r.status_code == 400
    assert "no longer active" in (r.json().get("message") or "").lower()


@pytest.mark.django_db
def test_validate_referral_rejects_capped_sponsor(system_config):
    capped = _member("+918100000002", account_status=User.AccountStatus.CAPPED)
    client = APIClient()
    r = client.post(
        "/api/v1/auth/validate-referral/",
        {"referral_code": capped.referral_code},
        format="json",
    )
    assert r.status_code == 404
    assert "no longer active" in (r.json().get("message") or "").lower()


@pytest.mark.django_db
def test_validate_public_slot_from_capped_issuer(system_config):
    capped = _member("+918100000003", account_status=User.AccountStatus.CAPPED)
    redeemer = _member("+918100000004")
    slot = _slot_code(capped)
    client = APIClient()
    client.force_authenticate(user=redeemer)
    r = client.post(
        "/api/v1/sponsor-slots/validate/",
        {"sponsor_code": slot.code},
        format="json",
    )
    assert r.status_code == 400
    assert r.json()["data"]["reason"] == "sponsor_inactive"


@pytest.mark.django_db
def test_cart_checkout_ignores_slot_from_capped_issuer(system_config, fake_razorpay_client):
    from apps.courses.models import EBook

    capped = _member("+918100000005", account_status=User.AccountStatus.CAPPED)
    redeemer = _member("+918100000006")
    slot = _slot_code(capped, code="CAPSLOT02")
    EBook.objects.create(
        title="Cap Book",
        slug="cap-book",
        category="Business",
        description="d",
        pages_count=10,
        language="English",
        price=Decimal("200"),
        status=EBook.Status.PUBLISHED,
        file_url="https://example.com/x.pdf",
        is_active=True,
    )
    client = APIClient()
    client.force_authenticate(user=redeemer)
    assert (
        client.post("/api/v1/user/cart/items/", {"ebook_slug": "cap-book"}, format="json").status_code
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
def test_capped_sponsor_cannot_manual_place(system_config):
    capped = _member(
        "+918100000007",
        account_status=User.AccountStatus.CAPPED,
        kyc_verified=True,
    )
    capped.kyc_first_approved_at = timezone.now()
    capped.save(update_fields=["kyc_first_approved_at"])
    BinaryTreeService.place_member(capped, None)
    buyer = _member("+918100000008", sponsor=capped)
    order = _paid_order(buyer, suffix="m")
    placement_mod.open_placement_queue_if_needed(order, buyer)
    client = APIClient()
    client.force_authenticate(user=capped)
    r = client.post(
        "/api/v1/user/tree/place-direct/",
        {"member_id": buyer.member_id, "leg": "LEFT"},
        format="json",
    )
    assert r.status_code == 400
    assert "earning cap" in (r.json().get("message") or "").lower()
    assert not hasattr(buyer, "binary_node")


@pytest.mark.django_db
def test_auto_place_reassigns_sponsor_when_capped(system_config, django_capture_on_commit_callbacks):
    _company_admin()
    company = company_fallback_sponsor()
    assert company is not None
    # Company fallback sponsor must itself be in the binary tree to host placements.
    BinaryTreeService.place_member(company, None)

    capped = _member("+918100000009", account_status=User.AccountStatus.CAPPED, sponsor=company)
    capped.is_member = True
    capped.save(update_fields=["is_member"])
    BinaryTreeService.place_member_manual_leg(capped, company, BinaryNode.Position.LEFT)

    buyer = _member("+918100000010", sponsor=capped)
    order = _paid_order(buyer, suffix="a")
    order.placement_status = Order.PlacementStatus.PENDING
    order.placement_deadline_at = timezone.now() - timedelta(minutes=1)
    order.save(update_fields=["placement_status", "placement_deadline_at"])

    with django_capture_on_commit_callbacks(execute=True):
        auto_place_pending_placements()

    buyer.refresh_from_db()
    assert buyer.sponsor_id == company.id
    assert hasattr(buyer, "binary_node")
    order.refresh_from_db()
    assert order.placement_status == Order.PlacementStatus.PLACED_AUTO


@pytest.mark.django_db
def test_admin_place_rejects_capped_parent(system_config):
    admin = User.objects.create_user(
        login_identifier="cap-bt-admin@test.dev",
        password="pw",
        email="cap-bt-admin@test.dev",
        full_name="BT Admin",
        member_id="CAPBTADM",
        referral_code="CAPBT01",
        referral_link="http://localhost/join?ref=CAPBT01",
        role=User.Role.SUPER_ADMIN,
        is_staff=True,
    )
    capped = _member("+918100000011", account_status=User.AccountStatus.CAPPED)
    capped.is_member = True
    capped.save(update_fields=["is_member"])
    BinaryTreeService.place_member(capped, None)

    root = _member("+918100000012", sponsor=None)
    root.is_member = True
    root.save(update_fields=["is_member"])
    BinaryTreeService.place_member(root, None)

    buyer = _member("+918100000013", sponsor=root)
    order = _paid_order(buyer, suffix="adm")
    placement_mod.complete_placement_for_order(
        order,
        manual_leg=BinaryNode.Position.LEFT,
        auto_strategy=None,
        final_status=Order.PlacementStatus.PLACED_MANUAL,
        actor=admin,
        audit_action="placement.test",
    )

    client = APIClient()
    client.force_authenticate(user=admin)
    r = client.post(
        f"/api/v1/admin/binary-tree/placements/{order.id}/place/",
        {"parent_member_id": capped.member_id, "leg": "RIGHT"},
        format="json",
    )
    assert r.status_code == 400
    assert "earning cap" in (r.json().get("message") or "").lower()


@pytest.mark.django_db
def test_auth_me_capped_strips_referral_and_sets_profile_message(system_config):
    capped = _member(
        "+918100000014",
        account_status=User.AccountStatus.CAPPED,
        kyc_verified=True,
    )
    Wallet.objects.get_or_create(user=capped, defaults={"total_earned": Decimal("22200")})
    client = APIClient()
    client.force_authenticate(user=capped)
    r = client.get("/api/v1/auth/me/")
    assert r.status_code == 200, r.content
    data = r.json()["data"]
    assert data["referral_code"] is None
    assert data["account_status"]["referral_link"] is None
    assert data["account_status"]["referral_link_active"] is False
    assert "profile_message" in data
    assert "earning cap" in data["profile_message"].lower()


@pytest.mark.django_db
def test_auth_me_active_has_no_profile_message(system_config):
    active = _member("+918100000015", kyc_verified=True)
    client = APIClient()
    client.force_authenticate(user=active)
    r = client.get("/api/v1/auth/me/")
    assert r.status_code == 200
    assert "profile_message" not in r.json()["data"]
