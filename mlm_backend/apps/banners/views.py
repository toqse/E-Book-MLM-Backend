from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.common.permissions import IsAdminRole
from apps.common.responses import envelope_response
from apps.common.url_utils import public_media_url

from .models import Banner


def _media_url(request, file_field):
    return public_media_url(request, file_field)


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _banner_payload(request, b: Banner) -> dict:
    return {
        "id": b.id,
        "title": b.title or "",
        "image_url": _media_url(request, b.image),
        "link_url": (b.link_url or "").strip() or "",
        "sort_order": int(b.sort_order or 0),
        "is_active": bool(b.is_active),
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "updated_at": b.updated_at.isoformat() if b.updated_at else None,
    }


def _validate_and_apply_banner_input(request, b: Banner, *, partial: bool):
    data = request.data or {}
    files = request.FILES
    errors: dict[str, str] = {}

    if not partial:
        if "image" not in files:
            errors["image"] = "This field is required."

    if "title" in data:
        b.title = str(data.get("title") or "").strip()

    if "link_url" in data:
        b.link_url = str(data.get("link_url") or "").strip()

    if "sort_order" in data:
        try:
            b.sort_order = int(str(data.get("sort_order") or "0").strip() or "0")
            if b.sort_order < 0:
                raise ValueError("sort_order")
        except Exception:
            errors["sort_order"] = "sort_order must be a non-negative integer."

    if "is_active" in data:
        b.is_active = _coerce_bool(data.get("is_active"))

    if "image" in files:
        b.image = files.get("image")

    if errors:
        return errors
    return None


@api_view(["GET"])
@permission_classes([AllowAny])
def public_banners(request):
    qs = Banner.objects.filter(is_active=True).order_by("sort_order", "-id")
    user = getattr(request, "user", None)
    if getattr(user, "is_authenticated", False):
        loginned_user = (getattr(user, "full_name", None) or str(user)).strip() or "Guest User"
    else:
        loginned_user = "Guest User"

    return Response(
        {
            "success": True,
            "data": {"results": [_banner_payload(request, b) for b in qs]},
            "message": "Operation successful",
            "errors": None,
            "loginned_user": loginned_user,
        }
    )


@api_view(["GET", "POST"])
@permission_classes([IsAdminRole])
def admin_banners(request):
    if request.method == "GET":
        qs = Banner.objects.all().order_by("sort_order", "-id")
        return envelope_response({"results": [_banner_payload(request, b) for b in qs]})

    b = Banner()
    errors = _validate_and_apply_banner_input(request, b, partial=False)
    if errors:
        return envelope_response(None, message="Validation failed", success=False, errors=errors, status=400)
    b.save()
    return envelope_response(_banner_payload(request, b), message="Created", status=201)


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAdminRole])
def admin_banner_detail(request, pk: int):
    b = Banner.objects.filter(pk=pk).first()
    if not b:
        return envelope_response(None, message="Not found", success=False, status=404)

    if request.method == "GET":
        return envelope_response(_banner_payload(request, b))

    if request.method == "PATCH":
        errors = _validate_and_apply_banner_input(request, b, partial=True)
        if errors:
            return envelope_response(None, message="Validation failed", success=False, errors=errors, status=400)
        b.save()
        return envelope_response(_banner_payload(request, b), message="Updated")

    # DELETE
    b.delete()
    return envelope_response({"deleted": True})

