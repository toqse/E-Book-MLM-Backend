from django.db import transaction
from rest_framework.decorators import api_view, permission_classes
from rest_framework.request import Request

from apps.admin_panel.models import SystemConfig
from apps.common.permissions import IsAdminRole
from apps.common.responses import envelope_response
from apps.payments.models import Order
from apps.users.models import User

from .placement import (
    admin_may_change_placement_in_cooloff,
    admin_reverse_placement,
    complete_placement_for_order,
 )


@api_view(["GET"])
@permission_classes([IsAdminRole])
def admin_placements_pending(request: Request):
    qs = (
        Order.objects.filter(
            placement_status__in=(
                Order.PlacementStatus.PENDING,
                Order.PlacementStatus.FAILED,
            ),
            status=Order.Status.PAID,
            is_retail_purchase=False,
        )
        .select_related("user", "user__sponsor")
        .order_by("placement_deadline_at", "id")[:200]
    )
    data = [
        {
            "order_id": o.id,
            "order_number": o.order_number,
            "buyer_member_id": o.user.member_id,
            "buyer_name": o.user.full_name,
            "sponsor_member_id": o.user.sponsor.member_id if o.user.sponsor_id else None,
            "placement_deadline_at": (
                o.placement_deadline_at.isoformat() if o.placement_deadline_at else None
            ),
            "placement_status": o.placement_status,
        }
        for o in qs
    ]
    return envelope_response({"results": data, "count": len(data)})


@api_view(["POST"])
@permission_classes([IsAdminRole])
def admin_placement_reverse(request: Request, order_id: int):
    order = Order.objects.filter(pk=order_id).select_related("user").first()
    if not order:
        return envelope_response(None, message="Order not found", success=False, status=404)
    ok, err = admin_may_change_placement_in_cooloff(order)
    if not ok:
        return envelope_response(None, message=err, success=False, status=400)
    try:
        with transaction.atomic():
            Order.objects.select_for_update().filter(pk=order.pk).first()
            admin_reverse_placement(order=order, actor=request.user)
    except ValueError as e:
        return envelope_response(None, message=str(e), success=False, status=409)
    return envelope_response({"order_id": order.id, "status": "reversed"})


@api_view(["POST"])
@permission_classes([IsAdminRole])
def admin_placement_reassign(request: Request, order_id: int):
    order = Order.objects.filter(pk=order_id).select_related("user").first()
    if not order:
        return envelope_response(None, message="Order not found", success=False, status=404)
    ok, err = admin_may_change_placement_in_cooloff(order)
    if not ok:
        return envelope_response(None, message=err, success=False, status=400)
    leg = (request.data.get("leg") or "").strip().upper() or None
    strategy = (request.data.get("strategy") or "").strip().upper() or None
    if leg and leg not in ("LEFT", "RIGHT"):
        return envelope_response(None, message="leg must be LEFT or RIGHT", success=False, status=400)
    allowed_strategies = {c.value for c in SystemConfig.AutoPlacementStrategy}
    if strategy and strategy not in allowed_strategies:
        return envelope_response(
            None,
            message="Invalid strategy",
            success=False,
            status=400,
        )
    if not leg and not strategy:
        return envelope_response(
            None,
            message="Provide leg (LEFT/RIGHT) or strategy (e.g. LEFT_FIRST)",
            success=False,
            status=400,
        )
    buyer = order.user
    try:
        with transaction.atomic():
            Order.objects.select_for_update().filter(pk=order.pk).first()
            if hasattr(buyer, "binary_node"):
                admin_reverse_placement(order=order, actor=request.user)
                order.refresh_from_db()
            if leg:
                sponsor = buyer.sponsor
                if not sponsor:
                    return envelope_response(
                        None,
                        message="Buyer has no sponsor for manual leg",
                        success=False,
                        status=400,
                    )
                complete_placement_for_order(
                    order,
                    manual_leg=leg,
                    auto_strategy=None,
                    final_status=Order.PlacementStatus.PLACED_ADMIN,
                    actor=request.user,
                    audit_action="placement.admin",
                )
            else:
                complete_placement_for_order(
                    order,
                    manual_leg=None,
                    auto_strategy=strategy,
                    final_status=Order.PlacementStatus.PLACED_ADMIN,
                    actor=request.user,
                    audit_action="placement.admin",
                )
    except ValueError as e:
        return envelope_response(None, message=str(e), success=False, status=409)
    return envelope_response({"order_id": order.id, "status": "reassigned"})
