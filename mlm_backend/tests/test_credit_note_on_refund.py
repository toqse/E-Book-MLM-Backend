"""Credit notes issued on refund approval and net GST in Finance Admin."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.payments import refund_request_views as refund_views
from apps.payments.models import CreditNote, GSTInvoice, Order, OrderLine, RefundRequest
from apps.payments.services import (
    apply_approved_refund_fulfillment,
    finalize_order_as_paid,
)
from apps.users.models import User
from apps.users.services import allocate_member_identity

from tests.test_refund_requests import (
    _finance_admin,
    _paid_order_with_window,
    _published_ebook,
    _published_ebook_pair,
    _two_line_paid_order,
)


@pytest.fixture
def razorpay_refund_stub_ok(monkeypatch):
    def fake(order, *, refund_reference, amount_inr):
        return "rfnd_cn_stub"

    monkeypatch.setattr(refund_views, "refund_razorpay_payment_for_order", fake)


@pytest.mark.django_db
def test_full_refund_creates_credit_note(system_config, razorpay_refund_stub_ok):
    mid, ref, link = allocate_member_identity()
    buyer = User(
        phone="+918080801099",
        full_name="CN Buyer",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
    )
    buyer.set_unusable_password()
    buyer.save()
    ebook = _published_ebook()
    order = _paid_order_with_window(buyer, ebook)

    client = APIClient()
    client.force_authenticate(user=buyer)
    client.post(f"/api/v1/user/orders/{order.pk}/refund/", {}, format="json")
    rr = RefundRequest.objects.get(order=order)

    fac = APIClient()
    fac.force_authenticate(user=_finance_admin())
    assert fac.post(f"/api/v1/admin/refunds/{rr.id}/approve/", {}, format="json").status_code == 200

    cn = CreditNote.objects.get(refund_request=rr)
    assert cn.base_amount == Decimal("200.00")
    assert cn.total_gst == Decimal("36.00")
    assert cn.cgst == Decimal("18.00")
    assert cn.sgst == Decimal("18.00")
    assert cn.grand_total == Decimal("236.00")
    assert cn.gst_invoice.order_id == order.pk
    assert cn.credit_note_number.startswith("CN-FY")
    assert AuditLog.objects.filter(action="credit_note.issued").exists()


@pytest.mark.django_db
def test_multi_line_partial_refund_credit_note_gst(system_config, razorpay_refund_stub_ok):
    mid, ref, link = allocate_member_identity()
    buyer = User(
        phone="+918080801098",
        full_name="CN Multi",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
    )
    buyer.set_unusable_password()
    buyer.save()
    eb1, eb2 = _published_ebook_pair()
    order = _two_line_paid_order(buyer, eb1, eb2)
    line1 = OrderLine.objects.get(order=order, ebook=eb1)

    client = APIClient()
    client.force_authenticate(user=buyer)
    client.post(
        f"/api/v1/user/orders/{order.pk}/refund/",
        {"order_line_id": line1.pk},
        format="json",
    )
    rr = RefundRequest.objects.get(order=order, order_line_id=line1.pk)

    fac = APIClient()
    fac.force_authenticate(user=_finance_admin())
    assert fac.post(f"/api/v1/admin/refunds/{rr.id}/approve/", {}, format="json").status_code == 200

    cn = CreditNote.objects.get(refund_request=rr)
    assert cn.base_amount == Decimal("200.00")
    assert cn.total_gst == Decimal("36.00")


@pytest.mark.django_db
def test_zero_paid_order_refund_skips_credit_note(system_config, razorpay_refund_stub_ok):
    mid, ref, link = allocate_member_identity()
    buyer = User(
        phone="+918080801097",
        full_name="CN Zero",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
    )
    buyer.set_unusable_password()
    buyer.save()
    ebook = _published_ebook()
    now = timezone.now()
    order = Order.objects.create(
        user=buyer,
        ebook=ebook,
        order_number=f"ORD-CN-ZERO-{buyer.id}",
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("241.72"),
        amount_paid=Decimal("0"),
        is_sponsor_slot_redemption=True,
        status=Order.Status.PAID,
        paid_at=now,
        refund_eligible_until=now + timedelta(days=7),
        razorpay_payment_id="pay_zero_slot",
    )
    GSTInvoice.objects.create(
        order=order,
        invoice_number=f"INV-ZERO-{order.pk}",
        base_amount=Decimal("200"),
        cgst=Decimal("18"),
        sgst=Decimal("18"),
        total_gst=Decimal("36"),
        grand_total=Decimal("236"),
    )
    rr = RefundRequest.objects.create(
        reference=f"RET-ZERO-{order.pk}",
        order=order,
        user=buyer,
        amount=Decimal("0"),
        status=RefundRequest.Status.PENDING,
    )
    apply_approved_refund_fulfillment(
        order=order,
        rr=rr,
        actor=None,
        razorpay_refund_id=None,
    )
    assert not CreditNote.objects.filter(refund_request=rr).exists()


@pytest.mark.django_db
def test_refund_without_invoice_skips_credit_note(system_config):
    mid, ref, link = allocate_member_identity()
    buyer = User(
        phone="+918080801096",
        full_name="CN No Inv",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
    )
    buyer.set_unusable_password()
    buyer.save()
    ebook = _published_ebook()
    now = timezone.now()
    order = Order.objects.create(
        user=buyer,
        ebook=ebook,
        order_number=f"ORD-CN-NOINV-{buyer.id}",
        base_price=Decimal("200"),
        gst_amount=Decimal("36"),
        gateway_charge=Decimal("5.72"),
        total_amount=Decimal("241.72"),
        discount_amount=Decimal("0"),
        amount_paid=Decimal("241.72"),
        status=Order.Status.PAID,
        paid_at=now,
        refund_eligible_until=now + timedelta(days=7),
        razorpay_payment_id="MANUAL-no-invoice",
    )
    rr = RefundRequest.objects.create(
        reference=f"RET-NOINV-{order.pk}",
        order=order,
        user=buyer,
        amount=Decimal("241.72"),
        payment_method=RefundRequest.PaymentMethod.DIRECT,
        status=RefundRequest.Status.PENDING,
    )
    admin = _finance_admin()
    apply_approved_refund_fulfillment(
        order=order,
        rr=rr,
        actor=admin,
        razorpay_refund_id=None,
    )
    assert not CreditNote.objects.filter(refund_request=rr).exists()
    assert AuditLog.objects.filter(action="credit_note.skipped_no_invoice").exists()
    order.refresh_from_db()
    assert order.status == Order.Status.REFUNDED
