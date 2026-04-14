import pytest
from django.contrib.auth import get_user_model

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
        },
    )


@pytest.fixture
def primary_ebook(db):
    return EBook.objects.create(
        title="Primary Course",
        slug="primary-course",
        category="Business",
        file_url="https://example.com/ebook.pdf",
        is_primary=True,
        is_active=True,
    )


@pytest.fixture
def member_user(db):
    mid, ref, link = allocate_member_identity()
    u = User(
        phone="9999999999",
        full_name="Test User",
        member_id=mid,
        referral_code=ref,
        referral_link=link,
    )
    u.set_unusable_password()
    u.save()
    return u
