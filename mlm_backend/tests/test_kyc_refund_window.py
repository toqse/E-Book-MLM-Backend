from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.admin_panel.models import SystemConfig
from apps.admin_panel.utils import get_system_config
from apps.courses.models import EBook
from apps.notifications.models import NotificationLog
from apps.payments.models import Order
from apps.payments.services import finalize_order_as_paid
from apps.users.models import User
from apps.users.services import allocate_member_identity
from apps.users.tasks import send_kyc_invitation_for_user, send_kyc_invitations_after_refund


def _member(phone: str) -> User:
    mid, ref, link = allocate_member_identity()
    u = User(
        phone=phone,
        full_name="KYC Test",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
    )
    u.set_unusable_password()
    u.save()
    return u


def _paid_order(user: User, ebook: EBook, *, refund_until=None) -> Order:
    o = Order.objects.create(
        user=user,
        ebook=ebook,
        order_number=f"ORD-KYC-{user.id}-{timezone.now().timestamp()}",
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("241.72"),
        is_retail_purchase=False,
        status=Order.Status.CREATED,
        razorpay_order_id="ord_kyc_test",
    )
    finalize_order_as_paid(o, payment_id="pay_kyc_test")
    o.refresh_from_db()
    if refund_until is not None:
        o.refund_eligible_until = refund_until
        o.save(update_fields=["refund_eligible_until"])
    return o


@pytest.mark.django_db
def test_compliance_submit_blocked_before_refund_window(system_config, primary_ebook):
    cfg = get_system_config()
    cfg.trigger_instant_kyc_submission = False
    cfg.save(update_fields=["trigger_instant_kyc_submission"])

    user = _member("+919200000001")
    _paid_order(user, primary_ebook)

    client = APIClient()
    client.force_authenticate(user=user)
    r = client.post("/api/v1/auth/compliance/submit/", {}, format="multipart")
    assert r.status_code == 403
    assert r.json()["errors"]["detail"] == "kyc_refund_window_active"


@pytest.mark.django_db
def test_compliance_submit_allowed_after_refund_window(system_config, primary_ebook):
    cfg = get_system_config()
    cfg.trigger_instant_kyc_submission = False
    cfg.save(update_fields=["trigger_instant_kyc_submission"])

    user = _member("+919200000002")
    past = timezone.now() - timedelta(days=1)
    _paid_order(user, primary_ebook, refund_until=past)

    client = APIClient()
    client.force_authenticate(user=user)
    r = client.post("/api/v1/auth/compliance/submit/", {}, format="multipart")
    assert r.status_code != 403 or r.json().get("errors", {}).get("detail") != "kyc_refund_window_active"


@pytest.mark.django_db
def test_instant_mode_allows_submit_after_purchase(system_config, primary_ebook):
    cfg = get_system_config()
    cfg.trigger_instant_kyc_submission = True
    cfg.save(update_fields=["trigger_instant_kyc_submission"])

    user = _member("+919200000003")
    _paid_order(user, primary_ebook)

    client = APIClient()
    client.force_authenticate(user=user)
    r = client.post("/api/v1/auth/compliance/submit/", {}, format="multipart")
    assert r.status_code != 403 or r.json().get("errors", {}).get("detail") != "kyc_refund_window_active"


@pytest.mark.django_db
def test_me_feature_access_before_and_after_kyc(system_config, primary_ebook):
    cfg = get_system_config()
    cfg.trigger_instant_kyc_submission = True
    cfg.save(update_fields=["trigger_instant_kyc_submission"])

    user = _member("+919200000004")
    _paid_order(user, primary_ebook)

    client = APIClient()
    client.force_authenticate(user=user)
    r = client.get("/api/v1/auth/me/")
    data = r.json()["data"]
    assert data["account_status"]["kyc_submission_allowed"] is True
    assert data["account_status"]["kyc_submission_mode"] == "instant"
    assert data["feature_access"]["compliance_submit"] is True
    assert data["feature_access"]["team_network"] is False
    assert data["kyc_notice"]["code"] == "submit_kyc"


@pytest.mark.django_db
def test_kyc_invitation_skipped_in_instant_mode(system_config, primary_ebook):
    cfg = get_system_config()
    cfg.trigger_instant_kyc_submission = True
    cfg.save(update_fields=["trigger_instant_kyc_submission"])

    user = _member("+919200000005")
    past = timezone.now() - timedelta(days=1)
    _paid_order(user, primary_ebook, refund_until=past)

    assert send_kyc_invitation_for_user(user.pk) is False
    user.refresh_from_db()
    assert user.kyc_invitation_sent_at is None
    assert NotificationLog.objects.filter(user=user, template_key="kyc_invitation").count() == 0


@pytest.mark.django_db
def test_kyc_invitation_sent_in_standard_mode(system_config, primary_ebook):
    cfg = get_system_config()
    cfg.trigger_instant_kyc_submission = False
    cfg.save(update_fields=["trigger_instant_kyc_submission"])

    user = _member("+919200000006")
    past = timezone.now() - timedelta(days=1)
    _paid_order(user, primary_ebook, refund_until=past)

    assert send_kyc_invitation_for_user(user.pk) is True
    user.refresh_from_db()
    assert user.kyc_invitation_sent_at is not None
    assert NotificationLog.objects.filter(user=user, template_key="kyc_invitation").exists()


@pytest.mark.django_db
def test_kyc_invite_validate_token(system_config, primary_ebook):
    from apps.agreements.kyc_invite_token import build_kyc_invite_token

    user = _member("+919200000007")
    past = timezone.now() - timedelta(days=1)
    _paid_order(user, primary_ebook, refund_until=past)

    token = build_kyc_invite_token(user_id=user.pk)
    client = APIClient()
    r = client.get(f"/api/v1/auth/kyc/invite/?token={token}")
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["valid"] is True
    assert body["member_id"] == user.member_id
    assert body["redirect_hint"] == "compliance"


@pytest.mark.django_db
def test_celery_batch_skips_instant_mode(system_config, primary_ebook):
    cfg = get_system_config()
    cfg.trigger_instant_kyc_submission = True
    cfg.save(update_fields=["trigger_instant_kyc_submission"])

    user = _member("+919200000008")
    past = timezone.now() - timedelta(days=1)
    _paid_order(user, primary_ebook, refund_until=past)

    assert send_kyc_invitations_after_refund() == 0


@pytest.mark.django_db
def test_dashboard_blocked_without_kyc(system_config, primary_ebook):
    cfg = get_system_config()
    cfg.trigger_instant_kyc_submission = True
    cfg.save(update_fields=["trigger_instant_kyc_submission"])

    user = _member("+919200000009")
    _paid_order(user, primary_ebook)

    client = APIClient()
    client.force_authenticate(user=user)
    r = client.get("/api/v1/user/dashboard/")
    assert r.status_code == 403
    assert r.json()["errors"]["detail"] in ("kyc_required", "compliance_profile_required")


@pytest.mark.django_db
def test_me_and_dashboard_unlocked_after_kyc_verified(system_config, primary_ebook):
    from apps.agreements.models import MemberComplianceProfile

    cfg = get_system_config()
    cfg.trigger_instant_kyc_submission = True
    cfg.save(update_fields=["trigger_instant_kyc_submission"])

    user = _member("+919200000010")
    _paid_order(user, primary_ebook)
    user.kyc_status = User.KYCStatus.VERIFIED
    user.save(update_fields=["kyc_status"])
    MemberComplianceProfile.objects.create(user=user)

    client = APIClient()
    client.force_authenticate(user=user)

    me = client.get("/api/v1/auth/me/")
    assert me.status_code == 200
    data = me.json()["data"]
    assert data["referral_code"] is not None
    assert data["earning_cap"] is not None
    assert data["team_legs"] is not None
    assert data["tax_withholding"] is not None
    assert data["feature_access"]["referral_program"] is True

    dash = client.get("/api/v1/user/dashboard/")
    assert dash.status_code == 200
    assert dash.json()["data"].get("wallet") is not None


def _support_admin():
    mid, ref, link = allocate_member_identity()
    admin = User(
        phone="+919200009999",
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


@pytest.mark.django_db
def test_admin_send_kyc_invitation_single(system_config, primary_ebook):
    cfg = get_system_config()
    cfg.trigger_instant_kyc_submission = False
    cfg.save(update_fields=["trigger_instant_kyc_submission"])

    user = _member("+919200000011")
    past = timezone.now() - timedelta(days=1)
    _paid_order(user, primary_ebook, refund_until=past)

    client = APIClient()
    client.force_authenticate(user=_support_admin())
    r = client.post(f"/api/v1/admin/users/{user.pk}/kyc/send-invitation/", {}, format="json")
    assert r.status_code == 200, r.content
    data = r.json()["data"]
    assert data["sent"] is True
    assert data.get("link")
    user.refresh_from_db()
    assert user.kyc_invitation_sent_at is not None


@pytest.mark.django_db
def test_admin_resend_kyc_invitation_with_force(system_config, primary_ebook):
    cfg = get_system_config()
    cfg.trigger_instant_kyc_submission = False
    cfg.save(update_fields=["trigger_instant_kyc_submission"])

    user = _member("+919200000012")
    past = timezone.now() - timedelta(days=1)
    _paid_order(user, primary_ebook, refund_until=past)

    client = APIClient()
    client.force_authenticate(user=_support_admin())

    r1 = client.post(f"/api/v1/admin/users/{user.pk}/kyc/send-invitation/", {}, format="json")
    assert r1.status_code == 200

    r2 = client.post(
        f"/api/v1/admin/users/{user.pk}/kyc/send-invitation/",
        {"force": True},
        format="json",
    )
    assert r2.status_code == 200
    assert r2.json()["data"]["resent"] is True


@pytest.mark.django_db
def test_admin_send_kyc_invitation_already_sent_without_force(system_config, primary_ebook):
    cfg = get_system_config()
    cfg.trigger_instant_kyc_submission = False
    cfg.save(update_fields=["trigger_instant_kyc_submission"])

    user = _member("+919200000013")
    past = timezone.now() - timedelta(days=1)
    _paid_order(user, primary_ebook, refund_until=past)

    client = APIClient()
    client.force_authenticate(user=_support_admin())
    client.post(f"/api/v1/admin/users/{user.pk}/kyc/send-invitation/", {}, format="json")

    r = client.post(f"/api/v1/admin/users/{user.pk}/kyc/send-invitation/", {}, format="json")
    assert r.status_code == 400
    assert r.json()["errors"]["detail"] == "already_sent"


@pytest.mark.django_db
def test_admin_bulk_send_kyc_invitation(system_config, primary_ebook):
    cfg = get_system_config()
    cfg.trigger_instant_kyc_submission = False
    cfg.save(update_fields=["trigger_instant_kyc_submission"])

    u1 = _member("+919200000014")
    u2 = _member("+919200000015")
    past = timezone.now() - timedelta(days=1)
    _paid_order(u1, primary_ebook, refund_until=past)
    _paid_order(u2, primary_ebook, refund_until=past)

    client = APIClient()
    client.force_authenticate(user=_support_admin())
    r = client.post(
        "/api/v1/admin/users/kyc/send-invitation/",
        {"user_ids": [u1.pk, u2.pk]},
        format="json",
    )
    assert r.status_code == 200
    body = r.json()["data"]
    assert len(body["sent"]) == 2
    assert body["failed"] == []
