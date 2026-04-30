import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.courses.models import EBook
from apps.users.models import User


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

