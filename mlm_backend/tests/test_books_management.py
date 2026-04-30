import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.courses.models import EBook, Enrollment
from apps.payments.models import Order
from apps.users.models import User
from apps.payments import services as payment_services


def _admin_user() -> User:
    return User.objects.create_user(
        login_identifier="books-admin@test.dev",
        password="pw",
        email="books-admin@test.dev",
        full_name="Books Admin",
        member_id="SUP000222",
        referral_code="SUP222",
        referral_link="http://localhost/join?ref=SUP222",
        role=User.Role.SUPPORT,
        is_staff=True,
    )


def _sample_image():
    return SimpleUploadedFile(
        "thumb.png",
        (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
            b"\x00\x00\x0bIDAT\x08\xd7c\xf8\x0f\x00\x01\x01\x01"
            b"\x00\x18\xdd\x8d\xb1\x00\x00\x00\x00IEND\xaeB`\x82"
        ),
        content_type="image/png",
    )


def _sample_pdf(name: str):
    return SimpleUploadedFile(name, b"%PDF-1.4\n%sample\n", content_type="application/pdf")


def _member_user(login_identifier: str, member_id: str, referral_code: str, phone: str) -> User:
    return User.objects.create_user(
        login_identifier=login_identifier,
        password="pw",
        full_name="Member User",
        member_id=member_id,
        referral_code=referral_code,
        referral_link=f"http://localhost/join?ref={referral_code}",
        phone=phone,
    )


@pytest.mark.django_db
def test_admin_can_create_book_with_required_fields():
    admin = _admin_user()
    client = APIClient()
    client.force_authenticate(user=admin)

    payload = {
        "title": "How To MLM",
        "slug": "how-to-mlm",
        "category": "Business",
        "description": "Complete guide",
        "pages_count": 256,
        "language": "English",
        "price": "399.00",
        "status": "PUBLISHED",
        "thumbnail": _sample_image(),
        "full_pdf": _sample_pdf("full.pdf"),
        "preview_pdf": _sample_pdf("preview.pdf"),
    }
    resp = client.post("/api/v1/admin/courses/", payload, format="multipart")

    assert resp.status_code == 201, resp.content
    book = EBook.objects.get(slug="how-to-mlm")
    assert book.status == EBook.Status.PUBLISHED
    assert str(book.price) == "399.00"
    assert book.is_active is True


@pytest.mark.django_db
def test_draft_books_hidden_from_public_list_and_checkout():
    EBook.objects.create(
        title="Draft Book",
        slug="draft-book",
        category="Business",
        description="Draft",
        pages_count=5,
        language="English",
        price=150,
        status=EBook.Status.DRAFT,
        file_url="https://example.com/draft.pdf",
        is_active=False,
    )
    public_client = APIClient()
    list_resp = public_client.get("/api/v1/courses/")
    assert list_resp.status_code == 200
    slugs = [x["slug"] for x in list_resp.json()["data"]["results"]]
    assert "draft-book" not in slugs

    buyer = User.objects.create_user(
        login_identifier="+919800000001",
        password="pw",
        full_name="Buyer User",
        member_id="MBR100001",
        referral_code="MBR001",
        referral_link="http://localhost/join?ref=MBR001",
        phone="+919800000001",
    )
    buyer_client = APIClient()
    buyer_client.force_authenticate(user=buyer)
    order_resp = buyer_client.post(
        "/api/v1/payments/create-order/",
        {"ebook_slug": "draft-book"},
        format="json",
    )
    assert order_resp.status_code == 404


@pytest.mark.django_db
def test_course_detail_pdf_url_by_access_level_slug_and_id():
    book = EBook.objects.create(
        title="Access Book",
        slug="access-book",
        category="Business",
        description="Access control",
        pages_count=50,
        language="English",
        price=100,
        status=EBook.Status.PUBLISHED,
        file_url="https://example.com/legacy.pdf",
        is_active=True,
        full_pdf=SimpleUploadedFile(
            "full_access.pdf",
            b"%PDF-1.4 full",
            content_type="application/pdf",
        ),
        preview_pdf=SimpleUploadedFile(
            "preview_access.pdf",
            b"%PDF-1.4 preview",
            content_type="application/pdf",
        ),
    )

    anon = APIClient()
    resp_slug_anon = anon.get(f"/api/v1/courses/{book.slug}/")
    resp_id_anon = anon.get(f"/api/v1/courses/{book.id}/")
    assert resp_slug_anon.status_code == 200
    assert resp_id_anon.status_code == 200
    assert "/ebooks/preview/" in resp_slug_anon.json()["data"]["pdf_url"]
    assert "/ebooks/preview/" in resp_id_anon.json()["data"]["pdf_url"]

    non_buyer = _member_user("+919811111111", "MBR200001", "MBR201", "+919811111111")
    non_buyer_client = APIClient()
    non_buyer_client.force_authenticate(user=non_buyer)
    resp_slug_non = non_buyer_client.get(f"/api/v1/courses/{book.slug}/")
    resp_id_non = non_buyer_client.get(f"/api/v1/courses/{book.id}/")
    assert resp_slug_non.status_code == 200
    assert resp_id_non.status_code == 200
    assert "/ebooks/preview/" in resp_slug_non.json()["data"]["pdf_url"]
    assert "/ebooks/preview/" in resp_id_non.json()["data"]["pdf_url"]

    buyer = _member_user("+919822222222", "MBR200002", "MBR202", "+919822222222")
    order = Order.objects.create(
        user=buyer,
        ebook=book,
        order_number="ORD-TEST-BOOK-001",
        amount_paid="100.00",
        status=Order.Status.PAID,
    )
    Enrollment.objects.create(user=buyer, ebook=book, order=order, is_retail=False)

    buyer_client = APIClient()
    buyer_client.force_authenticate(user=buyer)
    resp_slug_buy = buyer_client.get(f"/api/v1/courses/{book.slug}/")
    resp_id_buy = buyer_client.get(f"/api/v1/courses/{book.id}/")
    assert resp_slug_buy.status_code == 200
    assert resp_id_buy.status_code == 200
    assert "/ebooks/full/" in resp_slug_buy.json()["data"]["pdf_url"]
    assert "/ebooks/full/" in resp_id_buy.json()["data"]["pdf_url"]


@pytest.mark.django_db
def test_course_detail_404_for_non_published_on_slug_and_id():
    book = EBook.objects.create(
        title="Hidden Draft",
        slug="hidden-draft",
        category="Business",
        description="Draft only",
        pages_count=5,
        language="English",
        price=10,
        status=EBook.Status.DRAFT,
        file_url="https://example.com/draft-only.pdf",
        is_active=False,
    )
    client = APIClient()
    assert client.get(f"/api/v1/courses/{book.slug}/").status_code == 404
    assert client.get(f"/api/v1/courses/{book.id}/").status_code == 404


@pytest.mark.django_db
def test_create_order_prefers_ebook_id_when_both_id_and_slug_present(monkeypatch):
    preferred = EBook.objects.create(
        title="Preferred Book",
        slug="preferred-book",
        category="Business",
        description="Preferred",
        pages_count=10,
        language="English",
        price=321,
        status=EBook.Status.PUBLISHED,
        file_url="https://example.com/preferred.pdf",
        is_active=True,
    )
    other = EBook.objects.create(
        title="Other Book",
        slug="other-book",
        category="Business",
        description="Other",
        pages_count=20,
        language="English",
        price=999,
        status=EBook.Status.PUBLISHED,
        file_url="https://example.com/other.pdf",
        is_active=True,
    )
    buyer = _member_user("+919833333333", "MBR200003", "MBR203", "+919833333333")
    buyer_client = APIClient()
    buyer_client.force_authenticate(user=buyer)

    class _FakeClient:
        class order:
            @staticmethod
            def create(payload):
                return {"id": "order_fake_123", "amount": payload["amount"]}

    monkeypatch.setattr(payment_services, "_client", lambda: _FakeClient())

    resp = buyer_client.post(
        "/api/v1/payments/create-order/",
        {"ebook_id": preferred.id, "ebook_slug": other.slug},
        format="json",
    )
    assert resp.status_code == 200, resp.content
    order_id = resp.json()["data"]["order_id"]
    order = Order.objects.get(pk=order_id)
    assert order.ebook_id == preferred.id

