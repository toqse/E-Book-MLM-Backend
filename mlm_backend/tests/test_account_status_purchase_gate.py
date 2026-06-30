from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.courses.models import EBook
from apps.payments.models import Order
from apps.payments.services import finalize_order_as_paid
from apps.users.models import User
from apps.users.services import allocate_member_identity, maybe_activate_account_on_purchase


def _member(phone: str, *, account_status: str | None = None) -> User:
    mid, ref, link = allocate_member_identity()
    kwargs = dict(
        phone=phone,
        full_name="Status Test",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
    )
    if account_status is not None:
        kwargs["account_status"] = account_status
    u = User(**kwargs)
    u.set_unusable_password()
    u.save()
    return u


def _paid_order(user: User, ebook: EBook) -> Order:
    o = Order.objects.create(
        user=user,
        ebook=ebook,
        order_number=f"ORD-STAT-{user.id}-{timezone.now().timestamp()}",
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("241.72"),
        is_retail_purchase=False,
        status=Order.Status.CREATED,
        razorpay_order_id=f"ord_stat_{user.id}",
    )
    return finalize_order_as_paid(o, payment_id=f"pay_stat_{user.id}")


@pytest.mark.django_db
def test_new_member_defaults_to_inactive():
    user = _member("+919300000001")
    assert user.account_status == User.AccountStatus.INACTIVE
    assert user.is_member is False


@pytest.mark.django_db
def test_paid_order_activates_inactive_member(system_config, primary_ebook):
    user = _member("+919300000002")
    _paid_order(user, primary_ebook)
    user.refresh_from_db()
    assert user.account_status == User.AccountStatus.ACTIVE
    assert user.is_member is True


@pytest.mark.django_db
def test_maybe_activate_noop_for_suspended(system_config, primary_ebook):
    user = _member("+919300000003", account_status=User.AccountStatus.SUSPENDED)
    _paid_order(user, primary_ebook)
    user.refresh_from_db()
    assert user.account_status == User.AccountStatus.SUSPENDED
    assert user.is_member is True


@pytest.mark.django_db
def test_maybe_activate_noop_for_capped(system_config, primary_ebook):
    user = _member("+919300000004", account_status=User.AccountStatus.CAPPED)
    changed = maybe_activate_account_on_purchase(user)
    assert changed is False
    assert user.account_status == User.AccountStatus.CAPPED


@pytest.mark.django_db
def test_admin_unsuspend_restores_inactive_without_purchase(system_config):
    User.objects.create_superuser(
        "admin-stat@test.dev",
        "pw",
        full_name="Admin",
        email="admin-stat@test.dev",
    )
    user = _member("+919300000005", account_status=User.AccountStatus.SUSPENDED)

    client = APIClient()
    admin = User.objects.get(email="admin-stat@test.dev")
    client.force_authenticate(user=admin)

    r = client.post(f"/api/v1/admin/users/{user.pk}/unsuspend/", {}, format="json")
    assert r.status_code == 200, r.content
    user.refresh_from_db()
    assert user.account_status == User.AccountStatus.INACTIVE


@pytest.mark.django_db
def test_admin_unsuspend_restores_active_with_purchase(system_config, primary_ebook):
    User.objects.create_superuser(
        "admin-stat2@test.dev",
        "pw",
        full_name="Admin",
        email="admin-stat2@test.dev",
    )
    user = _member("+919300000006")
    _paid_order(user, primary_ebook)
    user.account_status = User.AccountStatus.SUSPENDED
    user.save(update_fields=["account_status"])

    client = APIClient()
    admin = User.objects.get(email="admin-stat2@test.dev")
    client.force_authenticate(user=admin)

    r = client.post(f"/api/v1/admin/users/{user.pk}/unsuspend/", {}, format="json")
    assert r.status_code == 200, r.content
    user.refresh_from_db()
    assert user.account_status == User.AccountStatus.ACTIVE
