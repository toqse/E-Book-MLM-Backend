import itertools

import pytest
from django.contrib.auth import get_user_model

_identity_seq = itertools.count(1)


def unique_test_pan(*, seq: int | None = None) -> str:
    """Valid unique PAN for tests (ABCDE####L)."""
    n = seq if seq is not None else next(_identity_seq)
    letter = chr(ord("A") + (n % 26))
    return f"ABCDE{1000 + (n % 9000):04d}{letter}"


def unique_test_aadhaar(*, seq: int | None = None) -> str:
    """Valid unique 12-digit Aadhaar for tests."""
    n = seq if seq is not None else next(_identity_seq)
    return f"{123412340000 + (n % 1_000_000):012d}"[-12:]

from apps.admin_panel.models import SystemConfig
from apps.courses.models import EBook
from apps.users.services import allocate_member_identity

User = get_user_model()


@pytest.fixture
def system_config(db):
    SystemConfig.objects.get_or_create(
        pk=1,
        defaults={
            "product_base_price": 200,
            "gst_rate": 0.18,
            "direct_commission": 30,
            "upline_commission": 10,
            "earning_cap": 22200,
            "placement_manual_window_hours": 24,
            "auto_placement_strategy": SystemConfig.AutoPlacementStrategy.LEFT_FIRST,
        },
    )


@pytest.fixture
def primary_ebook(db):
    return EBook.objects.create(
        title="Primary Course",
        slug="primary-course",
        category="Business",
        description="Primary course",
        pages_count=120,
        language="English",
        price=200,
        status=EBook.Status.PUBLISHED,
        file_url="https://example.com/ebook.pdf",
        is_primary=True,
        is_active=True,
    )


@pytest.fixture
def member_user(db):
    mid, ref, link = allocate_member_identity()
    u = User(
        phone="+919999999999",
        email="member-test@example.com",
        full_name="Test User",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
    )
    u.set_unusable_password()
    u.save()
    return u
