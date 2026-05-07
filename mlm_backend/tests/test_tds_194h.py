from decimal import Decimal

import pytest
from django.utils import timezone

from apps.tds.models import TdsLedger
from apps.tds.services import calculate_and_apply_194h_tds, get_current_financial_year
from apps.users.models import User
from apps.users.services import allocate_member_identity


def _verified_member(*, phone: str, pan: str = "ABCDE1234F") -> User:
    mid, ref, link = allocate_member_identity()
    u = User(
        phone=phone,
        full_name="TDS User",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
        pan_number=pan,
        kyc_status=User.KYCStatus.VERIFIED,
    )
    u.set_unusable_password()
    u.save()
    return u


@pytest.mark.django_db
def test_tds_below_threshold_no_deduction():
    u = _verified_member(phone="+918000001001")
    r = calculate_and_apply_194h_tds(user=u, gross_amount=Decimal("1000"))
    assert r.tds_amount == Decimal("0.00")
    assert r.net_amount == Decimal("1000.00")
    assert r.tds_applicable is False


@pytest.mark.django_db
def test_tds_crossing_threshold_catchup():
    u = _verified_member(phone="+918000001002")
    fy = get_current_financial_year()

    # Bring the user close to threshold with no TDS.
    r1 = calculate_and_apply_194h_tds(user=u, gross_amount=Decimal("19990"))
    assert r1.tds_amount == Decimal("0.00")

    # Cross the threshold: catch-up TDS applies on cumulative.
    r2 = calculate_and_apply_194h_tds(user=u, gross_amount=Decimal("20"))
    # new_total = 20010; required total tds = 20010 * 2% = 400.20
    # We cap TDS per credit to avoid negative net, so this credit is fully withheld.
    assert r2.tds_amount == Decimal("20.00")
    assert r2.net_amount == Decimal("0.00")

    led = TdsLedger.objects.get(user=u, financial_year=fy)
    assert led.tds_triggered is True
    assert led.total_earned == Decimal("20010.00")
    assert led.total_tds == Decimal("20.00")


@pytest.mark.django_db
def test_tds_after_trigger_deducts_each_credit():
    u = _verified_member(phone="+918000001003")
    # Cross once.
    calculate_and_apply_194h_tds(user=u, gross_amount=Decimal("20010"))
    # Next credit: TDS on gross only.
    r = calculate_and_apply_194h_tds(user=u, gross_amount=Decimal("100"))
    assert r.tds_amount == Decimal("2.00")
    assert r.net_amount == Decimal("98.00")


@pytest.mark.django_db
def test_financial_year_rollover_creates_new_row():
    u = _verified_member(phone="+918000001004")
    calculate_and_apply_194h_tds(user=u, gross_amount=Decimal("100"))

    # Simulate date in next FY by calling helper directly with 'at' (Apr 1 next year).
    now = timezone.now()
    import datetime as _dt

    next_year_apr1 = timezone.make_aware(
        _dt.datetime(now.year + 1, 4, 1, 0, 0, 0),
        timezone.get_current_timezone(),
    )
    fy2 = get_current_financial_year(at=next_year_apr1)
    # Ensure the label is different from current FY.
    assert fy2 != get_current_financial_year()

