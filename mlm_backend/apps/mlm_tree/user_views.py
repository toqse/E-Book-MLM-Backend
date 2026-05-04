from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request

from apps.common.responses import envelope_response
from apps.payments.models import Order
from apps.users.models import User

from .placement import (
    complete_placement_for_order,
    get_pending_placement_order,
    sponsor_may_manual_place,
)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def tree_place_direct(request: Request):
    """Sponsor places a paid direct referral on LEFT or RIGHT leg (within manual window)."""
    member_id = (request.data.get("member_id") or "").strip()
    leg = (request.data.get("leg") or request.data.get("position") or "").strip().upper()
    if not member_id:
        return envelope_response(
            None,
            message="member_id is required",
            success=False,
            status=400,
        )
    if leg not in ("LEFT", "RIGHT"):
        return envelope_response(
            None,
            message="leg must be LEFT or RIGHT",
            success=False,
            status=400,
        )
    buyer = User.objects.filter(member_id__iexact=member_id).first()
    if not buyer:
        return envelope_response(None, message="Member not found", success=False, status=404)
    order = get_pending_placement_order(buyer)
    if not order:
        return envelope_response(
            None,
            message="No pending placement for this member",
            success=False,
            status=400,
        )
    try:
        with transaction.atomic():
            Order.objects.select_for_update().filter(pk=order.pk).first()
            ok, err = sponsor_may_manual_place(request.user, buyer, order)
            if not ok:
                return envelope_response(None, message=err, success=False, status=400)
            complete_placement_for_order(
                order,
                manual_leg=leg,
                auto_strategy=None,
                final_status=Order.PlacementStatus.PLACED_MANUAL,
                actor=request.user,
                audit_action="placement.manual",
            )
    except (ValueError, RuntimeError) as exc:
        return envelope_response(None, message=str(exc), success=False, status=400)
    except ObjectDoesNotExist:
        return envelope_response(
            None,
            message="Binary tree is not ready for this sponsor; contact support.",
            success=False,
            status=400,
        )
    return envelope_response(
        {"member_id": buyer.member_id, "leg": leg, "order_id": order.id},
        message="Placed",
    )
