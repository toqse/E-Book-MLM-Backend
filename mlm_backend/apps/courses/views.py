from datetime import timedelta

from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated

from apps.common.permissions import IsAdminRole
from apps.common.responses import envelope_response

from .models import EBook, Enrollment


@api_view(["GET"])
@permission_classes([AllowAny])
def list_ebooks(request):
    qs = EBook.objects.filter(is_active=True)
    data = [
        {
            "slug": b.slug,
            "title": b.title,
            "category": b.category,
            "is_primary": b.is_primary,
        }
        for b in qs
    ]
    return envelope_response({"results": data})


@api_view(["GET"])
@permission_classes([AllowAny])
def ebook_detail(request, slug: str):
    b = EBook.objects.filter(slug=slug, is_active=True).first()
    if not b:
        return envelope_response(None, message="Not found", success=False, status=404)
    return envelope_response(
        {"slug": b.slug, "title": b.title, "category": b.category, "file_url": b.file_url}
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_enrollments(request):
    qs = Enrollment.objects.filter(user=request.user).select_related("ebook")
    data = [{"slug": e.ebook.slug, "title": e.ebook.title} for e in qs]
    return envelope_response({"results": data})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def download_signed(request, slug: str):
    b = EBook.objects.filter(slug=slug).first()
    if not b:
        return envelope_response(None, message="Not found", success=False, status=404)
    if not Enrollment.objects.filter(user=request.user, ebook=b).exists():
        return envelope_response(None, message="Not enrolled", success=False, status=403)
    url = b.file_url
    exp = (timezone.now() + timedelta(minutes=15)).isoformat()
    return envelope_response({"download_url": url, "expires_at": exp})


@api_view(["GET", "POST"])
@permission_classes([IsAdminRole])
def admin_course_list(request):
    if request.method == "GET":
        qs = EBook.objects.all()
        return envelope_response(
            {
                "results": [
                    {"id": b.id, "slug": b.slug, "title": b.title, "is_active": b.is_active}
                    for b in qs
                ]
            }
        )
    EBook.objects.create(
        title=request.data.get("title", "Course"),
        slug=request.data.get("slug", "course"),
        category=request.data.get("category", "General"),
        file_url=request.data.get("file_url", "https://example.com/file.pdf"),
        is_primary=bool(request.data.get("is_primary", False)),
    )
    return envelope_response({"ok": True})


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAdminRole])
def admin_course_detail(request, pk: int):
    b = EBook.objects.filter(pk=pk).first()
    if not b:
        return envelope_response(None, message="Not found", success=False, status=404)
    if request.method == "PATCH":
        for field in ["title", "slug", "category", "file_url", "is_active", "is_primary"]:
            if field in request.data:
                setattr(b, field, request.data[field])
        b.save()
        return envelope_response({"ok": True})
    b.is_active = False
    b.save(update_fields=["is_active"])
    return envelope_response({"deleted": True})


@api_view(["GET"])
@permission_classes([IsAdminRole])
def admin_enrollments(request):
    qs = Enrollment.objects.all()[:200]
    return envelope_response({"count": qs.count()})
