from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.db.models import Case, Count, F, IntegerField, Q, Sum, Value, When
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.request import Request

from apps.admin_panel.utils import get_system_config
from apps.common.permissions import IsAdminRole
from apps.common.responses import envelope_response
from apps.payments.models import Order
from apps.users.models import User
from apps.users import team_services

from .models import BinaryNode
from .placement import admin_may_change_placement_in_cooloff, admin_place_under_parent

# Tree view: depth limits response size and DB work (full binary tree grows ~2^(d+1) nodes).
ADMIN_BINARY_TREE_DEPTH_DEFAULT = 2
ADMIN_BINARY_TREE_DEPTH_MAX = 10


def _parse_int(raw: Any, *, default: int, lo: int, hi: int) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def _paginate(qs, request: Request, *, default_size: int = 50, max_size: int = 200):
    page = _parse_int(request.query_params.get("page"), default=1, lo=1, hi=10_000)
    page_size = _parse_int(
        request.query_params.get("page_size"), default=default_size, lo=1, hi=max_size
    )
    offset = (page - 1) * page_size
    return qs[offset : offset + page_size], page, page_size


@api_view(["GET"])
@permission_classes([IsAdminRole])
def admin_binary_tree_dashboard(request: Request):
    cfg = get_system_config()
    now = timezone.now()

    pending_orders = Order.objects.filter(
        placement_status__in=(Order.PlacementStatus.PENDING, Order.PlacementStatus.FAILED),
        status=Order.Status.PAID,
        is_retail_purchase=False,
    )
    pending_placements_count = pending_orders.count()

    # Empty slots = count of NULL left + NULL right pointers across all nodes.
    slots = BinaryNode.objects.aggregate(
        empty_left=Sum(
            Case(
                When(left_child__isnull=True, then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            )
        ),
        empty_right=Sum(
            Case(
                When(right_child__isnull=True, then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            )
        ),
    )
    empty_slots = int((slots.get("empty_left") or 0) + (slots.get("empty_right") or 0))

    # Imbalanced ratio heuristic using cached subtree sizes.
    # Avoid division: mark ratio > 3:1 when max >= 3*min and min>0.
    imbalanced_gt_3_1 = (
        BinaryNode.objects.annotate(
            strong=Case(
                When(left_subtree_size__gte=F("right_subtree_size"), then=F("left_subtree_size")),
                default=F("right_subtree_size"),
                output_field=IntegerField(),
            ),
            weak=Case(
                When(left_subtree_size__lt=F("right_subtree_size"), then=F("left_subtree_size")),
                default=F("right_subtree_size"),
                output_field=IntegerField(),
            ),
        )
        .filter(weak__gt=0, strong__gte=F("weak") * 3)
        .count()
    )

    unplaced_members_count = User.objects.filter(
        orders__status=Order.Status.PAID,
        orders__is_retail_purchase=False,
        binary_node__isnull=True,
    ).distinct().count()

    data = {
        "now": now.isoformat(),
        "config": {
            "refund_window_days": int(cfg.refund_window_days),
            "cooling_off_days": int(cfg.cooling_off_days),
            "placement_manual_window_hours": int(cfg.placement_manual_window_hours),
            "auto_placement_strategy": cfg.auto_placement_strategy,
        },
        "cards": {
            "pending_placements": pending_placements_count,
            "unplaced_members": unplaced_members_count,
            "empty_slots": empty_slots,
            "imbalanced_ratio_gt_3_1": imbalanced_gt_3_1,
        },
    }
    return envelope_response(data)


@api_view(["GET"])
@permission_classes([IsAdminRole])
def admin_binary_tree_pending_placements(request: Request):
    q = (request.query_params.get("q") or "").strip()
    qs = Order.objects.filter(
        placement_status__in=(Order.PlacementStatus.PENDING, Order.PlacementStatus.FAILED),
        status=Order.Status.PAID,
        is_retail_purchase=False,
    ).select_related("user", "user__sponsor")

    if q:
        qs = qs.filter(
            Q(order_number__icontains=q)
            | Q(user__member_id__icontains=q)
            | Q(user__full_name__icontains=q)
            | Q(user__sponsor__member_id__icontains=q)
            | Q(user__sponsor__full_name__icontains=q)
        )

    qs = qs.order_by("placement_deadline_at", "id")
    paged, page, page_size = _paginate(qs, request)

    now = timezone.now()
    results = []
    for o in paged:
        buyer = o.user
        sponsor = buyer.sponsor
        hrs = None
        if o.placement_deadline_at:
            hrs = max(0.0, (o.placement_deadline_at - now).total_seconds() / 3600.0)
        results.append(
            {
                "order_id": o.id,
                "order_number": o.order_number,
                "buyer": {
                    "user_id": buyer.id,
                    "member_id": buyer.member_id,
                    "full_name": buyer.full_name,
                    "joined_at": buyer.created_at.isoformat() if getattr(buyer, "created_at", None) else None,
                },
                "sponsor": (
                    {
                        "user_id": sponsor.id,
                        "member_id": sponsor.member_id,
                        "full_name": sponsor.full_name,
                    }
                    if sponsor
                    else None
                ),
                "placement_status": o.placement_status,
                "placement_deadline_at": o.placement_deadline_at.isoformat()
                if o.placement_deadline_at
                else None,
                "hours_remaining": hrs,
            }
        )
    return envelope_response(
        {
            "results": results,
            "count": qs.count(),
            "page": page,
            "page_size": page_size,
        }
    )


@api_view(["GET"])
@permission_classes([IsAdminRole])
def admin_binary_tree_tree_view(request: Request):
    raw_depth = request.query_params.get("depth")
    depth_requested: int | None = None
    if raw_depth is not None and str(raw_depth).strip() != "":
        try:
            depth_requested = int(raw_depth)
        except (TypeError, ValueError):
            depth_requested = None
    depth_effective = _parse_int(
        raw_depth,
        default=ADMIN_BINARY_TREE_DEPTH_DEFAULT,
        lo=0,
        hi=ADMIN_BINARY_TREE_DEPTH_MAX,
    )
    depth_capped = depth_requested is not None and depth_requested != depth_effective
    anchor_member_id = (request.query_params.get("anchor_member_id") or "").strip()

    meta = {
        "depth_requested": depth_requested,
        "depth_effective": depth_effective,
        "depth_max": ADMIN_BINARY_TREE_DEPTH_MAX,
        "depth_capped": depth_capped,
    }

    if anchor_member_id:
        anchor = User.objects.filter(member_id__iexact=anchor_member_id).first()
        if not anchor:
            return envelope_response(None, message="Anchor not found", success=False, status=404)
        payload = team_services.nested_tree_at_anchor_user(anchor, depth_effective)
        if isinstance(payload, dict):
            payload = {**payload, "tree_query": meta}
        return envelope_response(payload)

    roots = (
        BinaryNode.objects.filter(parent__isnull=True)
        .select_related("user")
        .order_by("id")[:50]
    )
    data = [team_services.nested_tree_at_anchor_user(r.user, depth_effective) for r in roots]
    for item in data:
        if isinstance(item, dict):
            item["tree_query"] = meta
    return envelope_response({"roots": data, "depth": depth_effective, "tree_query": meta})


@api_view(["GET"])
@permission_classes([IsAdminRole])
def admin_binary_tree_members_list(request: Request):
    q = (request.query_params.get("q") or "").strip()
    level = request.query_params.get("level")
    qs = BinaryNode.objects.select_related(
        "user", "user__sponsor", "parent", "parent__user"
    )

    if q:
        qs = qs.filter(Q(user__member_id__icontains=q) | Q(user__full_name__icontains=q))
    if level:
        try:
            lvl = int(level)
            qs = qs.filter(level=lvl)
        except (TypeError, ValueError):
            pass

    qs = qs.order_by("level", "id")
    paged, page, page_size = _paginate(qs, request, default_size=50, max_size=200)
    paged_list = list(paged)

    # Resolve representative placement_status per user: earliest paid non-retail order.
    # That row is the one the placement engine queues / resolves first.
    user_ids = [n.user_id for n in paged_list]
    placement_by_user: dict[int, str | None] = {}
    if user_ids:
        for uid, ps in (
            Order.objects.filter(
                user_id__in=user_ids,
                status=Order.Status.PAID,
                is_retail_purchase=False,
            )
            .order_by("user_id", "id")
            .values_list("user_id", "placement_status")
        ):
            if uid not in placement_by_user:
                placement_by_user[uid] = ps

    results = []
    for n in paged_list:
        u = n.user
        binary_parent_user = n.parent.user if n.parent_id else None
        sponsor_user = u.sponsor if u.sponsor_id else None
        left_dl = int(n.left_subtree_size or 0)
        right_dl = int(n.right_subtree_size or 0)
        if left_dl < right_dl:
            weak = "LEFT"
        elif right_dl < left_dl:
            weak = "RIGHT"
        else:
            weak = "BALANCED"
        results.append(
            {
                "user_id": u.id,
                "member_id": u.member_id,
                "full_name": u.full_name,
                "level": n.level,
                # referral_parent = the user who referred this member (their sponsor).
                # Null only when the member truly has no sponsor (e.g. company root admin).
                "referral_parent": (
                    {
                        "user_id": sponsor_user.id,
                        "member_id": sponsor_user.member_id,
                        "full_name": sponsor_user.full_name,
                    }
                    if sponsor_user
                    else None
                ),
                # binary_parent = the immediate parent node in the binary tree (may differ
                # from the sponsor due to spillover). Null only for binary-tree roots.
                "binary_parent": (
                    {
                        "user_id": binary_parent_user.id,
                        "member_id": binary_parent_user.member_id,
                        "full_name": binary_parent_user.full_name,
                    }
                    if binary_parent_user
                    else None
                ),
                "leg_position": n.position,
                "left_dl": left_dl,
                "right_dl": right_dl,
                "weak_leg": weak,
                "placement_status": placement_by_user.get(u.id),
                "status": {
                    "account_status": u.account_status,
                    "kyc_status": u.kyc_status,
                    "is_active": u.is_active,
                },
            }
        )
    return envelope_response(
        {"results": results, "count": qs.count(), "page": page, "page_size": page_size}
    )


@api_view(["GET"])
@permission_classes([IsAdminRole])
def admin_binary_tree_weak_leg_report(request: Request):
    cfg = get_system_config()
    page_size = _parse_int(request.query_params.get("page_size"), default=50, lo=1, hi=200)
    page = _parse_int(request.query_params.get("page"), default=1, lo=1, hi=10_000)
    offset = (page - 1) * page_size

    base = BinaryNode.objects.select_related("user").annotate(
        strong=Case(
            When(left_subtree_size__gte=F("right_subtree_size"), then=F("left_subtree_size")),
            default=F("right_subtree_size"),
            output_field=IntegerField(),
        ),
        weak=Case(
            When(left_subtree_size__lt=F("right_subtree_size"), then=F("left_subtree_size")),
            default=F("right_subtree_size"),
            output_field=IntegerField(),
        ),
        diff=Case(
            When(left_subtree_size__gte=F("right_subtree_size"), then=F("left_subtree_size") - F("right_subtree_size")),
            default=F("right_subtree_size") - F("left_subtree_size"),
            output_field=IntegerField(),
        ),
    )

    members_with_weak_leg = base.filter(diff__gt=0).count()
    imbalanced_ratio_gt_3_1 = base.filter(weak__gt=0, strong__gte=F("weak") * 3).count()

    pending_placements = Order.objects.filter(
        placement_status__in=(Order.PlacementStatus.PENDING, Order.PlacementStatus.FAILED),
        status=Order.Status.PAID,
        is_retail_purchase=False,
    ).count()

    slots = BinaryNode.objects.aggregate(
        empty_left=Sum(
            Case(
                When(left_child__isnull=True, then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            )
        ),
        empty_right=Sum(
            Case(
                When(right_child__isnull=True, then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            )
        ),
    )
    empty_slots = int((slots.get("empty_left") or 0) + (slots.get("empty_right") or 0))

    qs = base.order_by("-diff", "id")
    nodes = list(qs[offset : offset + page_size])
    results = []
    for n in nodes:
        left = int(n.left_subtree_size or 0)
        right = int(n.right_subtree_size or 0)
        weak_leg = "LEFT" if left < right else ("RIGHT" if right < left else "BALANCED")
        strong = max(left, right)
        weak = min(left, right)
        ratio = None
        if weak > 0:
            ratio = round(strong / weak, 2)
        suggested = "Place next in LEFT leg" if weak_leg == "LEFT" else "Place next in RIGHT leg"
        potential = Decimal(str(cfg.upline_commission))
        results.append(
            {
                "member": {
                    "user_id": n.user.id,
                    "member_id": n.user.member_id,
                    "full_name": n.user.full_name,
                    "level": n.level,
                },
                "strong_leg_count": strong,
                "weak_leg_count": weak,
                "weak_leg": weak_leg,
                "imbalance_ratio": ratio,
                "suggested_action": suggested,
                "potential_passive_unit": str(potential),
            }
        )

    return envelope_response(
        {
            "cards": {
                "members_with_weak_leg": members_with_weak_leg,
                "imbalanced_ratio_gt_3_1": imbalanced_ratio_gt_3_1,
                "pending_placements": pending_placements,
                "empty_slots": empty_slots,
                "potential_passive_unit": str(cfg.upline_commission),
            },
            "results": results,
            "count": qs.count(),
            "page": page,
            "page_size": page_size,
        }
    )


@api_view(["POST"])
@permission_classes([IsAdminRole])
def admin_binary_tree_place_under_parent(request: Request, order_id: int):
    order = Order.objects.filter(pk=order_id).select_related("user").first()
    if not order:
        return envelope_response(None, message="Order not found", success=False, status=404)
    ok, err = admin_may_change_placement_in_cooloff(order)
    if not ok:
        return envelope_response(None, message=err, success=False, status=400)

    parent_member_id = (request.data.get("parent_member_id") or "").strip()
    leg = (request.data.get("leg") or "").strip().upper()
    if not parent_member_id:
        return envelope_response(None, message="parent_member_id is required", success=False, status=400)
    if leg not in ("LEFT", "RIGHT"):
        return envelope_response(None, message="leg must be LEFT or RIGHT", success=False, status=400)
    parent = User.objects.filter(member_id__iexact=parent_member_id).first()
    if not parent:
        return envelope_response(None, message="Parent not found", success=False, status=404)

    try:
        from django.db import transaction

        with transaction.atomic():
            Order.objects.select_for_update().filter(pk=order.pk).first()
            admin_place_under_parent(
                order=order,
                parent_user=parent,
                leg=leg,
                actor=request.user,
            )
    except ValueError as exc:
        return envelope_response(None, message=str(exc), success=False, status=409)

    return envelope_response(
        {
            "order_id": order.id,
            "status": "placed",
            "parent_member_id": parent.member_id,
            "leg": leg,
        }
    )

