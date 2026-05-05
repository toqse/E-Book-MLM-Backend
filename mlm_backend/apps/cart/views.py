import logging

from django.db import IntegrityError
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request

from apps.common.responses import envelope_response
from apps.courses.models import EBook, Enrollment
from apps.payments.services import create_checkout_order_from_cart, normalize_billing_from_payload

from .models import Cart, CartItem
from .services import preview_checkout_totals

logger = logging.getLogger(__name__)


def _get_or_create_cart(user) -> Cart:
    cart, _ = Cart.objects.get_or_create(user=user)
    return cart


def _resolve_ebook_from_payload(data: dict) -> EBook | None:
    ebook_id = data.get("ebook_id")
    slug = data.get("ebook_slug")
    if ebook_id not in (None, ""):
        return EBook.objects.filter(pk=ebook_id, status=EBook.Status.PUBLISHED).first()
    if slug:
        return EBook.objects.filter(slug=slug, status=EBook.Status.PUBLISHED).first()
    return None


def _cart_payload(request: Request) -> dict:
    cart = _get_or_create_cart(request.user)
    items = list(
        CartItem.objects.filter(cart=cart)
        .select_related("ebook")
        .order_by("ebook_id", "id")
    )
    ebooks = [it.ebook for it in items]
    rows = [
        {
            "id": it.id,
            "ebook_id": it.ebook_id,
            "slug": it.ebook.slug,
            "title": it.ebook.title,
            "price": str(it.ebook.price),
        }
        for it in items
    ]
    return {
        "items": rows,
        "totals": preview_checkout_totals(ebooks),
    }


@api_view(["GET", "DELETE"])
@permission_classes([IsAuthenticated])
def cart_root(request: Request):
    if request.method == "DELETE":
        cart = Cart.objects.filter(user=request.user).first()
        if cart:
            CartItem.objects.filter(cart=cart).delete()
        return envelope_response({"cleared": True})
    return envelope_response(_cart_payload(request))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def cart_add_item(request: Request):
    ebook = _resolve_ebook_from_payload(request.data or {})
    if not ebook:
        return envelope_response(
            None,
            message="Book not found or not published",
            success=False,
            status=404,
        )
    if Enrollment.objects.filter(user=request.user, ebook=ebook).exists():
        return envelope_response(
            None,
            message="You are already enrolled in this book",
            success=False,
            status=400,
        )
    cart = _get_or_create_cart(request.user)
    try:
        CartItem.objects.create(cart=cart, ebook=ebook)
    except IntegrityError:
        logger.debug("cart_add_duplicate user=%s ebook=%s", request.user.pk, ebook.pk)
        return envelope_response(
            None,
            message="This book is already in your cart",
            success=False,
            status=400,
        )
    return envelope_response(_cart_payload(request))


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def cart_remove_item(request: Request, item_id: int):
    cart = Cart.objects.filter(user=request.user).first()
    if not cart:
        return envelope_response(None, message="Cart is empty", success=False, status=404)
    deleted, _ = CartItem.objects.filter(pk=item_id, cart=cart).delete()
    if not deleted:
        return envelope_response(None, message="Cart item not found", success=False, status=404)
    return envelope_response({"removed": True})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def cart_checkout(request: Request):
    from django.conf import settings

    cart = _get_or_create_cart(request.user)
    sponsor_code = request.data.get("sponsor_code") or request.data.get("sponsor_slot_code")
    is_retail = bool(request.data.get("is_retail", False))
    billing = normalize_billing_from_payload(request.data)

    try:
        order, rz = create_checkout_order_from_cart(
            request.user,
            cart,
            sponsor_code=sponsor_code,
            is_retail=is_retail,
            billing=billing,
        )
    except ValueError as e:
        return envelope_response(None, message=str(e), success=False, status=400)
    except RuntimeError as e:
        return envelope_response(None, message=str(e), success=False, status=500)
    except Exception as e:
        return envelope_response(None, message=str(e), success=False, status=500)

    if rz is None:
        return envelope_response(
            {
                "order_id": order.id,
                "order_number": order.order_number,
                "amount_paise": 0,
                "razorpay_order_id": None,
                "key_id": settings.RAZORPAY_KEY_ID,
                "status": order.status,
            }
        )
    return envelope_response(
        {
            "order_id": order.id,
            "order_number": order.order_number,
            "amount_paise": rz["amount"],
            "razorpay_order_id": rz["id"],
            "key_id": settings.RAZORPAY_KEY_ID,
        }
    )
