from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db.models import Count, Q
from django.db.utils import IntegrityError
from django.utils import timezone
from django.utils.text import slugify
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.common.permissions import IsAdminRole
from apps.common.responses import envelope_response

from .models import EBook, Enrollment


def _media_url(request, file_field):
    if not file_field:
        return None
    try:
        return request.build_absolute_uri(file_field.url)
    except Exception:
        return file_field.url


def _book_payload(request, b: EBook):
    return {
        "id": b.id,
        "slug": b.slug,
        "title": b.title,
        "category": b.category,
        "description": b.description,
        "thumbnail_url": _media_url(request, b.thumbnail),
        "pages_count": b.pages_count,
        "language": b.language,
        "price": str(b.price),
        "status": b.status,
        "is_primary": b.is_primary,
        "is_active": b.is_active,
        "full_pdf_url": _media_url(request, b.full_pdf),
        "preview_pdf_url": _media_url(request, b.preview_pdf),
    }


def _preview_full_urls(request, b: EBook) -> tuple:
    preview_url = _media_url(request, b.preview_pdf) or b.file_url
    full_url = _media_url(request, b.full_pdf) or b.file_url
    return preview_url, full_url


def _public_catalog_book_payload(
    request,
    b: EBook,
    enrolled_book_ids: set[int] | None = None,
) -> dict:
    payload = {
        "id": b.id,
        "slug": b.slug,
        "title": b.title,
        "category": b.category,
        "description": b.description,
        "thumbnail_url": _media_url(request, b.thumbnail),
        "pages_count": b.pages_count,
        "language": b.language,
        "price": str(b.price),
        "status": b.status,
        "is_primary": b.is_primary,
        "is_active": b.is_active,
    }
    if enrolled_book_ids is not None:
        payload["is_already_purchased"] = b.pk in enrolled_book_ids
    return payload


def _book_detail_payload(request, b: EBook):
    preview_url, full_url = _preview_full_urls(request, b)
    u = getattr(request, "user", None)
    auth = bool(u and getattr(u, "is_authenticated", False))
    enrolled = Enrollment.objects.filter(user=u, ebook=b).exists() if auth else False
    in_cart = False
    if auth:
        try:
            from apps.cart.models import CartItem

            in_cart = CartItem.objects.filter(cart__user=u, ebook=b).exists()
        except Exception:
            in_cart = False
    payload = {
        "id": b.id,
        "slug": b.slug,
        "title": b.title,
        "category": b.category,
        "description": b.description,
        "thumbnail_url": _media_url(request, b.thumbnail),
        "pages_count": b.pages_count,
        "language": b.language,
        "price": str(b.price),
        "status": b.status,
        "is_primary": b.is_primary,
        "is_active": b.is_active,
        "pdf_url": full_url if enrolled else preview_url,
        "is_already_in_cart": in_cart,
    }
    if auth:
        payload["is_already_purchased"] = enrolled
    return payload


def _is_uploaded_file(value):
    return hasattr(value, "name") and hasattr(value, "size")


def _file_ext_ok(value, allowed_exts: tuple[str, ...]) -> bool:
    if value is None:
        return False
    name = ""
    if _is_uploaded_file(value):
        name = value.name or ""
    elif isinstance(value, str):
        name = value
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ext in allowed_exts


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _parse_positive_int(raw: str | None, default: int, *, min_v: int = 1, max_v: int | None = None) -> int:
    try:
        v = int(raw.strip() if isinstance(raw, str) else (raw if raw is not None else default))
    except (TypeError, ValueError, AttributeError):
        v = default
    v = max(min_v, v)
    if max_v is not None:
        v = min(v, max_v)
    return v


def _list_search_term(request) -> str:
    raw = request.query_params.get("search") or request.query_params.get("q") or ""
    return raw.strip()


def _apply_course_list_filters(qs, request):
    category = (request.query_params.get("category") or "").strip()
    if category:
        qs = qs.filter(category__iexact=category)

    language = (request.query_params.get("language") or "").strip()
    if language:
        qs = qs.filter(language__iexact=language)

    term = _list_search_term(request)
    if term:
        qs = qs.filter(
            Q(title__icontains=term) | Q(category__icontains=term) | Q(language__icontains=term),
        )

    return qs


def _group_books_slice_by_category(
    books: list[EBook],
    request,
    enrolled_book_ids: set[int] | None = None,
) -> list[dict]:
    """Preserve order within `books`; merge consecutive rows with the same category string."""
    out: list[dict] = []
    for b in books:
        payload = _public_catalog_book_payload(request, b, enrolled_book_ids)
        if out and out[-1]["category"] == b.category:
            out[-1]["books"].append(payload)
        else:
            out.append({"category": b.category, "books": [payload]})
    return out


def _paginated_grouped_course_catalog(qs, request) -> dict:
    qs = qs.order_by("category", "-id")
    page = _parse_positive_int(request.query_params.get("page"), 1, min_v=1, max_v=10_000)
    page_size = _parse_positive_int(request.query_params.get("page_size"), 20, min_v=1, max_v=100)
    total_count = qs.count()
    start = (page - 1) * page_size
    page_objs = list(qs[start : start + page_size])
    total_pages = (total_count + page_size - 1) // page_size if total_count else 0
    enrolled_for_page: set[int] | None = None
    if getattr(request.user, "is_authenticated", False):
        page_ids = [b.pk for b in page_objs]
        enrolled_for_page = (
            set(
                Enrollment.objects.filter(user=request.user, ebook_id__in=page_ids).values_list(
                    "ebook_id",
                    flat=True,
                )
            )
            if page_ids
            else set()
        )
    data = _group_books_slice_by_category(page_objs, request, enrolled_for_page)
    return {
        "results": data,
        "count": total_count,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


def _apply_status_bridge(b: EBook):
    b.is_active = b.status == EBook.Status.PUBLISHED


def _make_unique_slug(raw: str) -> str:
    base = slugify(raw or "").strip("-")
    if not base:
        return ""
    base = base[:60].rstrip("-")
    slug = base
    i = 2
    while EBook.objects.filter(slug=slug).exists():
        suffix = f"-{i}"
        slug = f"{base[: (60 - len(suffix))].rstrip('-')}{suffix}"
        i += 1
    return slug


def _validate_and_apply_book_input(request, b: EBook, *, partial: bool):
    data = request.data
    files = request.FILES
    errors = {}

    required_fields = [
        "title",
        "category",
        "description",
        "pages_count",
        "language",
        "price",
        "status",
        "thumbnail",
        "full_pdf",
        "preview_pdf",
    ]

    if not partial:
        for field in required_fields:
            if field not in data and field not in files:
                errors[field] = "This field is required."

    def assign_text(field_name: str):
        if field_name in data:
            val = (data.get(field_name) or "").strip()
            if not val:
                errors[field_name] = "This field may not be blank."
            else:
                setattr(b, field_name, val)

    assign_text("title")
    assign_text("category")
    assign_text("description")
    assign_text("language")

    if "slug" in data:
        raw_slug = (data.get("slug") or "").strip()
        if not raw_slug:
            errors["slug"] = "Slug may not be blank."
        else:
            b.slug = raw_slug
    elif not partial:
        # Backward-compatible: allow admin client to omit slug and auto-generate it.
        if "title" in data:
            b.slug = _make_unique_slug(str(data.get("title") or ""))
            if not b.slug:
                errors["slug"] = "Unable to generate slug from title; please provide slug."

    if "pages_count" in data:
        try:
            pages_count = int(str(data.get("pages_count")).strip())
            if pages_count <= 0:
                raise ValueError("pages_count")
            b.pages_count = pages_count
        except Exception:
            errors["pages_count"] = "pages_count must be a positive integer."

    if "price" in data:
        try:
            price = Decimal(str(data.get("price")).strip())
            if price < 0:
                raise InvalidOperation
            b.price = price.quantize(Decimal("0.01"))
        except Exception:
            errors["price"] = "price must be a non-negative decimal number."

    if "status" in data:
        status_val = (data.get("status") or "").strip().upper()
        if status_val not in {EBook.Status.DRAFT, EBook.Status.PUBLISHED}:
            errors["status"] = "status must be DRAFT or PUBLISHED."
        else:
            b.status = status_val
            _apply_status_bridge(b)

    if "is_active" in data and "status" not in data:
        b.is_active = _coerce_bool(data.get("is_active"))
        b.status = EBook.Status.PUBLISHED if b.is_active else EBook.Status.DRAFT

    if "is_primary" in data:
        b.is_primary = _coerce_bool(data.get("is_primary"))

    if "thumbnail" in files:
        thumbnail = files.get("thumbnail")
        if not _file_ext_ok(thumbnail, ("jpg", "jpeg", "png", "webp")):
            errors["thumbnail"] = "thumbnail must be an image file (jpg/jpeg/png/webp)."
        else:
            b.thumbnail = thumbnail
    elif "thumbnail" in data and not partial:
        if not _file_ext_ok(data.get("thumbnail"), ("jpg", "jpeg", "png", "webp")):
            errors["thumbnail"] = "thumbnail must be an image URL or file path."

    if "full_pdf" in files:
        full_pdf = files.get("full_pdf")
        if not _file_ext_ok(full_pdf, ("pdf",)):
            errors["full_pdf"] = "full_pdf must be a PDF file."
        else:
            b.full_pdf = full_pdf
    elif "full_pdf" in data and not partial:
        if not _file_ext_ok(data.get("full_pdf"), ("pdf",)):
            errors["full_pdf"] = "full_pdf must be a PDF URL or file path."

    if "preview_pdf" in files:
        preview_pdf = files.get("preview_pdf")
        if not _file_ext_ok(preview_pdf, ("pdf",)):
            errors["preview_pdf"] = "preview_pdf must be a PDF file."
        else:
            b.preview_pdf = preview_pdf
    elif "preview_pdf" in data and not partial:
        if not _file_ext_ok(data.get("preview_pdf"), ("pdf",)):
            errors["preview_pdf"] = "preview_pdf must be a PDF URL or file path."

    if errors:
        return errors

    # Legacy bridge for older clients still passing file_url.
    if "file_url" in data:
        b.file_url = (data.get("file_url") or "").strip()
    elif b.file_url == "":
        b.file_url = "https://example.com/file.pdf"

    _apply_status_bridge(b)
    return None


@api_view(["GET"])
@permission_classes([AllowAny])
def list_ebooks(request):
    qs = EBook.objects.filter(status=EBook.Status.PUBLISHED)
    qs = _apply_course_list_filters(qs, request)
    data = _paginated_grouped_course_catalog(qs, request)
    if not getattr(request.user, "is_authenticated", False):
        return envelope_response(data)

    no_of_cart_items = 0
    try:
        from apps.cart.models import CartItem

        no_of_cart_items = int(CartItem.objects.filter(cart__user=request.user).count())
    except Exception:
        no_of_cart_items = 0

    # Keep the standard envelope, but add a common top-level field for authenticated users.
    return Response(
        {
            "success": True,
            "data": data,
            "message": "Operation successful",
            "errors": None,
            "no_of_cart_items": no_of_cart_items,
        },
        status=200,
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def bestsellers(request):
    """
    Public endpoint returning the top 3 best-selling published ebooks.
    "Sold" is computed as the number of Enrollment rows per ebook (created on successful PAID orders).
    """
    qs = (
        EBook.objects.filter(status=EBook.Status.PUBLISHED)
        .annotate(sold_count=Count("enrollments"))
        .order_by("-sold_count", "-id")
    )
    top = list(qs[:3])
    data = [
        {
            **_public_catalog_book_payload(request, b, enrolled_book_ids=None),
            "sold_count": int(getattr(b, "sold_count", 0) or 0),
        }
        for b in top
    ]
    return envelope_response({"results": data, "count": len(data)})


@api_view(["GET"])
@permission_classes([AllowAny])
def ebook_detail(request, slug: str):
    b = EBook.objects.filter(slug=slug, status=EBook.Status.PUBLISHED).first()
    if not b:
        return envelope_response(None, message="Not found", success=False, status=404)
    return envelope_response(_book_detail_payload(request, b))


@api_view(["GET"])
@permission_classes([AllowAny])
def ebook_detail_by_id(request, pk: int):
    b = EBook.objects.filter(pk=pk, status=EBook.Status.PUBLISHED).first()
    if not b:
        return envelope_response(None, message="Not found", success=False, status=404)
    return envelope_response(_book_detail_payload(request, b))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_enrollments(request):
    ebook_ids = Enrollment.objects.filter(user=request.user).values_list("ebook_id", flat=True)
    qs = EBook.objects.filter(pk__in=ebook_ids, status=EBook.Status.PUBLISHED)
    qs = _apply_course_list_filters(qs, request)
    return envelope_response(_paginated_grouped_course_catalog(qs, request))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_enrolled_ebook_detail(request, slug: str):
    b = EBook.objects.filter(slug=slug, status=EBook.Status.PUBLISHED).first()
    if not b:
        return envelope_response(None, message="Not found", success=False, status=404)
    if not Enrollment.objects.filter(user=request.user, ebook=b).exists():
        return envelope_response(None, message="Not enrolled", success=False, status=403)
    return envelope_response(_book_detail_payload(request, b))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_enrolled_ebook_detail_by_id(request, pk: int):
    b = EBook.objects.filter(pk=pk, status=EBook.Status.PUBLISHED).first()
    if not b:
        return envelope_response(None, message="Not found", success=False, status=404)
    if not Enrollment.objects.filter(user=request.user, ebook=b).exists():
        return envelope_response(None, message="Not enrolled", success=False, status=403)
    return envelope_response(_book_detail_payload(request, b))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def download_signed(request, slug: str):
    b = EBook.objects.filter(slug=slug, status=EBook.Status.PUBLISHED).first()
    if not b:
        return envelope_response(None, message="Not found", success=False, status=404)
    if not Enrollment.objects.filter(user=request.user, ebook=b).exists():
        return envelope_response(None, message="Not enrolled", success=False, status=403)
    url = _media_url(request, b.full_pdf) or b.file_url
    if not url:
        return envelope_response(None, message="Book PDF missing", success=False, status=400)
    exp = (timezone.now() + timedelta(minutes=15)).isoformat()
    return envelope_response({"download_url": url, "expires_at": exp})


@api_view(["GET", "POST"])
@permission_classes([IsAdminRole])
def admin_course_list(request):
    if request.method == "GET":
        qs = EBook.objects.all().order_by("-id")
        return envelope_response({"results": [_book_payload(request, b) for b in qs]})

    b = EBook(
        file_url="https://example.com/file.pdf",
        status=EBook.Status.DRAFT,
    )
    errors = _validate_and_apply_book_input(request, b, partial=False)
    if errors:
        return envelope_response(None, message="Validation failed", success=False, errors=errors, status=400)
    try:
        b.save()
    except IntegrityError as e:
        # Commonly triggered by missing/duplicate slug or other DB constraints.
        return envelope_response(
            None,
            message="Unable to create course",
            success=False,
            errors={"detail": str(e)},
            status=400,
        )
    return envelope_response(_book_payload(request, b), message="Created", status=201)


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAdminRole])
def admin_course_detail(request, pk: int):
    b = EBook.objects.filter(pk=pk).first()
    if not b:
        return envelope_response(None, message="Not found", success=False, status=404)
    if request.method == "PATCH":
        errors = _validate_and_apply_book_input(request, b, partial=True)
        if errors:
            return envelope_response(None, message="Validation failed", success=False, errors=errors, status=400)
        b.save()
        return envelope_response(_book_payload(request, b), message="Updated")
    b.is_active = False
    b.status = EBook.Status.DRAFT
    b.save(update_fields=["is_active", "status"])
    return envelope_response({"deleted": True})


@api_view(["GET"])
@permission_classes([IsAdminRole])
def admin_enrollments(request):
    qs = Enrollment.objects.all()[:200]
    return envelope_response({"count": qs.count()})
