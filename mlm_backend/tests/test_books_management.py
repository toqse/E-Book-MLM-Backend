import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from urllib.parse import urlparse
from rest_framework.test import APIClient

from apps.cart.models import Cart, CartItem
from apps.courses.models import EBook, Enrollment
from apps.payments.models import Order
from apps.users.models import User
from apps.payments import services as payment_services


def _flatten_courses_catalog_results(results):
    return [book for grp in results for book in grp["books"]]


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
    slugs = [x["slug"] for x in _flatten_courses_catalog_results(list_resp.json()["data"]["results"])]
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


def _minimal_published_ebook(slug: str, **kwargs):
    base = dict(
        title="Title",
        slug=slug,
        category="Business",
        description="Desc",
        pages_count=10,
        language="English",
        price=10,
        status=EBook.Status.PUBLISHED,
        file_url="https://example.com/x.pdf",
        is_active=True,
    )
    base.update(kwargs)
    return EBook.objects.create(**base)


@pytest.mark.django_db
def test_courses_list_pagination():
    client = APIClient()
    _minimal_published_ebook("c-a")
    _minimal_published_ebook("c-b")
    _minimal_published_ebook("c-c")

    r1 = client.get("/api/v1/courses/", {"page_size": "2", "page": "1"})
    assert r1.status_code == 200
    d1 = r1.json()["data"]
    assert d1["count"] == 3
    assert d1["page"] == 1
    assert d1["page_size"] == 2
    assert d1["total_pages"] == 2
    assert len(_flatten_courses_catalog_results(d1["results"])) == 2

    r2 = client.get("/api/v1/courses/", {"page_size": "2", "page": "2"})
    d2 = r2.json()["data"]
    assert len(_flatten_courses_catalog_results(d2["results"])) == 1


@pytest.mark.django_db
def test_courses_list_filters_and_search():
    client = APIClient()
    _minimal_published_ebook(
        "biz-en",
        title="Network Marketing Basics",
        category="Business",
        language="English",
    )
    _minimal_published_ebook(
        "health-hi",
        title="Nutrition Hindi",
        category="Health",
        language="Hindi",
    )

    by_cat = client.get("/api/v1/courses/", {"category": "health"})
    assert by_cat.status_code == 200
    cat_rows = by_cat.json()["data"]["results"]
    assert len(_flatten_courses_catalog_results(cat_rows)) == 1
    assert cat_rows[0]["category"] == "Health"
    assert cat_rows[0]["books"][0]["slug"] == "health-hi"

    by_lang = client.get("/api/v1/courses/", {"language": "hindi"})
    assert len(_flatten_courses_catalog_results(by_lang.json()["data"]["results"])) == 1

    search_title = client.get("/api/v1/courses/", {"search": "network"})
    ids = [x["slug"] for x in _flatten_courses_catalog_results(search_title.json()["data"]["results"])]
    assert "biz-en" in ids and "health-hi" not in ids

    search_cat = client.get("/api/v1/courses/", {"q": "health"})
    assert [x["slug"] for x in _flatten_courses_catalog_results(search_cat.json()["data"]["results"])] == ["health-hi"]

    combined = client.get("/api/v1/courses/", {"category": "Business", "search": "English"})
    comb_flat = _flatten_courses_catalog_results(combined.json()["data"]["results"])
    assert len(comb_flat) == 1
    assert comb_flat[0]["slug"] == "biz-en"


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
    data_slug_anon = resp_slug_anon.json()["data"]
    data_id_anon = resp_id_anon.json()["data"]
    assert "/ebooks/preview/" in data_slug_anon["pdf_url"]
    assert "/ebooks/preview/" in data_id_anon["pdf_url"]
    assert data_slug_anon["is_already_in_cart"] is False
    assert data_id_anon["is_already_in_cart"] is False
    assert "is_already_purchased" not in data_slug_anon
    assert "is_already_purchased" not in data_id_anon

    non_buyer = _member_user("+919811111111", "MBR200001", "MBR201", "+919811111111")
    non_buyer_client = APIClient()
    non_buyer_client.force_authenticate(user=non_buyer)
    resp_slug_non = non_buyer_client.get(f"/api/v1/courses/{book.slug}/")
    resp_id_non = non_buyer_client.get(f"/api/v1/courses/{book.id}/")
    assert resp_slug_non.status_code == 200
    assert resp_id_non.status_code == 200
    d_slug_non = resp_slug_non.json()["data"]
    d_id_non = resp_id_non.json()["data"]
    assert "/ebooks/preview/" in d_slug_non["pdf_url"]
    assert "/ebooks/preview/" in d_id_non["pdf_url"]
    assert d_slug_non["is_already_in_cart"] is False
    assert d_id_non["is_already_in_cart"] is False
    assert d_slug_non["is_already_purchased"] is False
    assert d_id_non["is_already_purchased"] is False

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
    d_slug_buy = resp_slug_buy.json()["data"]
    d_id_buy = resp_id_buy.json()["data"]
    assert "/api/v1/user/courses/access-book/download/" in d_slug_buy["pdf_url"]
    assert "token=" in d_slug_buy["pdf_url"]
    assert "/api/v1/user/courses/access-book/download/" in d_id_buy["pdf_url"]
    assert "token=" in d_id_buy["pdf_url"]
    assert d_slug_buy["is_already_in_cart"] is False
    assert d_id_buy["is_already_in_cart"] is False
    assert d_slug_buy["is_already_purchased"] is True
    assert d_id_buy["is_already_purchased"] is True


_CATALOG_BOOK_KEYS = {
    "id",
    "slug",
    "title",
    "category",
    "description",
    "thumbnail_url",
    "pages_count",
    "language",
    "price",
    "status",
    "is_primary",
    "is_active",
}
_CATALOG_BOOK_KEYS_AUTHENTICATED = _CATALOG_BOOK_KEYS | {"is_already_purchased"}
_CATALOG_BOOK_KEYS_ENROLLED = _CATALOG_BOOK_KEYS_AUTHENTICATED | {"pdf_url"}


@pytest.mark.django_db
def test_courses_list_grouped_by_category_full_book_payloads():
    owned = EBook.objects.create(
        title="Owned In List",
        slug="owned-in-list",
        category="Business",
        description="x",
        pages_count=10,
        language="English",
        price=50,
        status=EBook.Status.PUBLISHED,
        file_url="https://example.com/legacy2.pdf",
        is_active=True,
        full_pdf=SimpleUploadedFile("full_list.pdf", b"%PDF full", content_type="application/pdf"),
        preview_pdf=SimpleUploadedFile("preview_list.pdf", b"%PDF prv", content_type="application/pdf"),
    )
    EBook.objects.create(
        title="Other In List",
        slug="other-in-list",
        category="Trading",
        description="y",
        pages_count=5,
        language="English",
        price=99,
        status=EBook.Status.PUBLISHED,
        file_url="https://example.com/legacy3.pdf",
        is_active=True,
        full_pdf=SimpleUploadedFile("full_list2.pdf", b"%PDF full2", content_type="application/pdf"),
        preview_pdf=SimpleUploadedFile("preview_list2.pdf", b"%PDF prv2", content_type="application/pdf"),
    )

    anon = APIClient()
    anon_resp = anon.get("/api/v1/courses/")
    assert anon_resp.status_code == 200
    assert "no_of_cart_items" not in anon_resp.json()
    groups = anon_resp.json()["data"]["results"]
    assert {g["category"] for g in groups} == {"Business", "Trading"}
    for g in groups:
        assert "category_id" not in g
        for b in g["books"]:
            assert set(b.keys()) == _CATALOG_BOOK_KEYS
            assert "is_already_purchased" not in b
            assert "pdf_url" not in b
            assert "full_pdf_url" not in b
            assert "preview_pdf_url" not in b

    buyer = _member_user("+919844444444", "MBR200004", "MBR204", "+919844444444")
    order = Order.objects.create(
        user=buyer,
        ebook=owned,
        order_number="ORD-TEST-LIST-PDF",
        amount_paid="50.00",
        status=Order.Status.PAID,
    )
    Enrollment.objects.create(user=buyer, ebook=owned, order=order, is_retail=False)
    cart = Cart.objects.create(user=buyer)
    CartItem.objects.create(cart=cart, ebook=owned)

    auth_client = APIClient()
    auth_client.force_authenticate(user=buyer)
    auth_resp = auth_client.get("/api/v1/courses/")
    assert auth_resp.status_code == 200
    assert auth_resp.json()["no_of_cart_items"] == 1
    auth_by_slug = {}
    for g in auth_resp.json()["data"]["results"]:
        for b in g["books"]:
            assert set(b.keys()) == _CATALOG_BOOK_KEYS_AUTHENTICATED
            assert "pdf_url" not in b
            auth_by_slug[b["slug"]] = b
    assert auth_by_slug["owned-in-list"]["is_already_purchased"] is True
    assert auth_by_slug["other-in-list"]["is_already_purchased"] is False


@pytest.mark.django_db
def test_my_enrolled_list_matches_public_catalog_shape_and_dedupes_ebooks():
    biz = _minimal_published_ebook("my-biz", category="Business")
    health = _minimal_published_ebook("my-health", category="Health")
    buyer = _member_user("+919855555501", "MBR200501", "MBR501", "+919855555501")
    order1 = Order.objects.create(
        user=buyer,
        ebook=biz,
        order_number="ORD-EN-001",
        amount_paid="10.00",
        status=Order.Status.PAID,
    )
    order1b = Order.objects.create(
        user=buyer,
        ebook=biz,
        order_number="ORD-EN-002",
        amount_paid="10.00",
        status=Order.Status.PAID,
    )
    order2 = Order.objects.create(
        user=buyer,
        ebook=health,
        order_number="ORD-EN-003",
        amount_paid="10.00",
        status=Order.Status.PAID,
    )
    Enrollment.objects.create(user=buyer, ebook=biz, order=order1, is_retail=False)
    Enrollment.objects.create(user=buyer, ebook=biz, order=order1b, is_retail=False)
    Enrollment.objects.create(user=buyer, ebook=health, order=order2, is_retail=False)

    client = APIClient()
    client.force_authenticate(user=buyer)
    r = client.get("/api/v1/user/courses/enrolled/")
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["count"] == 2
    assert {g["category"] for g in d["results"]} == {"Business", "Health"}
    flat = _flatten_courses_catalog_results(d["results"])
    assert len(flat) == 2
    for b in flat:
        assert set(b.keys()) == _CATALOG_BOOK_KEYS_ENROLLED
        assert b["is_already_purchased"] is True
        assert b["pdf_url"]

    filtered = client.get("/api/v1/user/courses/enrolled/", {"category": "health"})
    assert filtered.json()["data"]["count"] == 1
    assert filtered.json()["data"]["results"][0]["category"] == "Health"


@pytest.mark.django_db
def test_my_enrolled_detail_matches_public_catalog_detail_and_requires_enrollment():
    book = EBook.objects.create(
        title="Mine",
        slug="mine-book",
        category="Business",
        description="x",
        pages_count=10,
        language="English",
        price=10,
        status=EBook.Status.PUBLISHED,
        file_url="https://example.com/m.pdf",
        is_active=True,
        full_pdf=SimpleUploadedFile("full_m.pdf", b"%PDF-1.4 full", content_type="application/pdf"),
        preview_pdf=SimpleUploadedFile("preview_m.pdf", b"%PDF-1.4 prv", content_type="application/pdf"),
    )
    owner = _member_user("+919855555502", "MBR200502", "MBR502", "+919855555502")
    order = Order.objects.create(
        user=owner,
        ebook=book,
        order_number="ORD-MINE",
        amount_paid="10.00",
        status=Order.Status.PAID,
    )
    Enrollment.objects.create(user=owner, ebook=book, order=order, is_retail=False)

    stranger = _member_user("+919855555503", "MBR200503", "MBR503", "+919855555503")
    s_client = APIClient()
    s_client.force_authenticate(user=stranger)
    assert s_client.get(f"/api/v1/user/courses/enrolled/{book.slug}/").status_code == 403
    assert s_client.get(f"/api/v1/user/courses/enrolled/{book.id}/").status_code == 403

    o_client = APIClient()
    o_client.force_authenticate(user=owner)
    r_slug = o_client.get(f"/api/v1/user/courses/enrolled/{book.slug}/")
    r_id = o_client.get(f"/api/v1/user/courses/enrolled/{book.id}/")
    assert r_slug.status_code == 200
    assert r_id.status_code == 200
    for resp in (r_slug, r_id):
        payload = resp.json()["data"]
        assert payload["slug"] == book.slug
        assert f"/api/v1/user/courses/{book.slug}/download/" in payload["pdf_url"]
        assert "token=" in payload["pdf_url"]
        assert payload["is_already_purchased"] is True


@pytest.mark.django_db
def test_enrolled_pdf_url_and_download_use_by_id_when_slug_blank_in_db():
    book = EBook.objects.create(
        title="Legacy Blank Slug Book",
        slug="legacy-blank-temp",
        category="HEALTH",
        description="x",
        pages_count=1,
        language="English",
        price=10,
        status=EBook.Status.PUBLISHED,
        file_url="https://example.com/x.pdf",
        is_active=True,
        full_pdf=_sample_pdf("fullBLANK.pdf"),
    )
    EBook.objects.filter(pk=book.pk).update(slug="")
    book.refresh_from_db()
    assert book.slug == ""

    buyer = _member_user("+919877700001", "MBR700001", "MBR701", "+919877700001")
    order = Order.objects.create(
        user=buyer,
        ebook=book,
        order_number="ORD-BLANK-SLUG",
        amount_paid="10.00",
        status=Order.Status.PAID,
    )
    Enrollment.objects.create(user=buyer, ebook=book, order=order, is_retail=False)

    client = APIClient()
    client.force_authenticate(user=buyer)
    r = client.get("/api/v1/user/courses/enrolled/")
    assert r.status_code == 200
    flat = _flatten_courses_catalog_results(r.json()["data"]["results"])
    row = next(b for b in flat if b["id"] == book.id)
    pdf_url = row["pdf_url"]
    assert f"/api/v1/user/courses/by-id-{book.pk}/download/" in pdf_url
    assert "token=" in pdf_url

    parsed = urlparse(pdf_url)
    dl_path = f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path
    anon = APIClient()
    r_pdf = anon.get(dl_path)
    assert r_pdf.status_code == 200
    assert r_pdf["Content-Type"].startswith("application/pdf")
    head = b"".join(r_pdf.streaming_content)[:8]
    assert head[:4] == b"%PDF"


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
        {
            "ebook_id": preferred.id,
            "ebook_slug": other.slug,
            "billing_line1": " Addr line 1 ",
            "billing_city": "Kochi",
            "billing_state": "KL",
            "billing_postal_code": "682001",
            "billing_country": "IN",
        },
        format="json",
    )
    assert resp.status_code == 200, resp.content
    order_id = resp.json()["data"]["order_id"]
    order = Order.objects.get(pk=order_id)
    assert order.ebook_id == preferred.id
    assert order.billing_line1 == "Addr line 1"
    assert order.billing_city == "Kochi"
    assert order.billing_postal_code == "682001"

