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
    admin_reverse_placement_subtree,
    build_dry_run_payload,
    collect_subtree_for_reverse,
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
    sponsor = buyer.sponsor
    if sponsor and not hasattr(sponsor, "binary_node"):
        return envelope_response(
            None,
            message="Sponsor is not placed in the binary tree yet; place the sponsor first.",
            success=False,
            status=400,
        )
    try:
        with transaction.atomic():
            Order.objects.select_for_update().filter(pk=order.pk).first()
            if hasattr(buyer, "binary_node"):
                admin_reverse_placement(order=order, actor=request.user)
                order.refresh_from_db()
            if leg:
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


@api_view(["POST"])
@permission_classes([IsAdminRole])
def admin_placement_reverse_subtree(request: Request, order_id: int):
    """
    Cascade-reverse the binary subtree rooted at this order's buyer.

    Modes:
      - Dry-run (?dry_run=true): read-only preview with full blast radius.
      - Execute (body { "confirm": true, expected_* }): performs the reverse
        atomically with TOCTOU protection via expected_* snapshot tokens.

    Existing /reverse/ endpoint remains leaf-only and is NOT changed.
    """
    order = Order.objects.filter(pk=order_id).select_related("user").first()
    if not order:
        return envelope_response(None, message="Order not found", success=False, status=404)
    ok, err = admin_may_change_placement_in_cooloff(order)
    if not ok:
        return envelope_response(None, message=err, success=False, status=400)

    dry_run = (request.query_params.get("dry_run") or "").strip().lower() == "true"

    if dry_run:
        try:
            preview = collect_subtree_for_reverse(order)
        except ValueError as e:
            return envelope_response(None, message=str(e), success=False, status=400)
        payload = build_dry_run_payload(preview)
        message = (
            "Dry-run preview. No changes were made."
            if preview.can_reverse
            else "Cannot reverse subtree: see `blocking` for details. No changes were made."
        )
        return envelope_response(
            payload, message=message, success=preview.can_reverse
        )

    confirm = request.data.get("confirm") is True
    if not confirm:
        return envelope_response(
            None,
            message="confirm must be true to execute subtree reverse",
            success=False,
            status=400,
        )

    expected_root = (request.data.get("expected_root_member_id") or "").strip()
    expected_count = request.data.get("expected_affected_count")
    expected_ids_raw = request.data.get("expected_affected_order_ids") or []
    try:
        expected_ids = [int(x) for x in expected_ids_raw]
    except (TypeError, ValueError):
        return envelope_response(
            None,
            message="expected_affected_order_ids must be a list of integers",
            success=False,
            status=400,
        )

    try:
        with transaction.atomic():
            Order.objects.select_for_update().filter(pk=order.pk).first()
            result = admin_reverse_placement_subtree(
                root_order=order,
                actor=request.user,
                expected_root_member_id=expected_root,
                expected_affected_count=(
                    int(expected_count) if expected_count is not None else None
                ),
                expected_affected_order_ids=expected_ids,
            )
    except ValueError as e:
        return envelope_response(None, message=str(e), success=False, status=409)
    return envelope_response(result, message="Subtree reversed")
