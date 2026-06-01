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
from apps.users.services import is_account_capped

from .models import BinaryNode
from .placement import admin_may_change_placement_in_cooloff, admin_place_under_parent

# Tree view: depth limits response size and DB work (full binary tree grows ~2^(d+1) nodes).
ADMIN_BINARY_TREE_DEPTH_DEFAULT = 2
ADMIN_BINARY_TREE_DEPTH_MAX = 10

# Tree-view server-side search ("q") caps; matches are computed in-memory
# over the already-loaded subtree, so no extra DB work.
ADMIN_BINARY_TREE_SEARCH_LIMIT_DEFAULT = 100
ADMIN_BINARY_TREE_SEARCH_LIMIT_MAX = 500


def _find_matched_pks(
    nodes_by_pk: dict[int, BinaryNode], q_lower: str
) -> set[int]:
    """Case-insensitive substring match on user.member_id and user.full_name."""
    matched: set[int] = set()
    for pk, n in nodes_by_pk.items():
        u = n.user
        mid = (getattr(u, "member_id", None) or "").lower()
        name = (getattr(u, "full_name", None) or "").lower()
        if q_lower in mid or q_lower in name:
            matched.add(pk)
    return matched


def _stamp_is_match(payload: dict[str, Any] | None, matched_member_ids: set[str]) -> None:
    """Walk a nested tree payload and set node['is_match'] for matched nodes.

    Also sets is_match=False on non-matching nodes so the field is always
    present (simpler for the client to consume)."""
    if not isinstance(payload, dict):
        return
    payload["is_match"] = payload.get("member_id") in matched_member_ids
    _stamp_is_match(payload.get("left"), matched_member_ids)
    _stamp_is_match(payload.get("right"), matched_member_ids)


def _build_search_matches(
    matched_pks: set[int],
    nodes_by_pk: dict[int, BinaryNode],
    anchor_pk: int | None,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    """Return (matches, total_count). ``matches`` is sorted by depth from the
    anchor (or root when anchor_pk is None), then by member_id, and capped at
    ``limit``. ``total_count`` is the unbounded match count.

    Each entry includes:
      - member_id, full_name
      - depth_from_anchor: int (0 = anchor itself; None if path could not be resolved)
      - path: list[str] of member_ids from anchor down to the matched member
    """
    if not matched_pks:
        return [], 0
    parent_by_child = team_services.build_parent_by_child(nodes_by_pk)

    rows: list[dict[str, Any]] = []
    for pk in matched_pks:
        n = nodes_by_pk.get(pk)
        if not n:
            continue
        u = n.user
        mid = getattr(u, "member_id", None)
        name = getattr(u, "full_name", None)

        # Walk up to the anchor (or until we lose the parent chain) to build
        # the path of member_ids and the depth.
        path_pks: list[int] = [pk]
        cur = pk
        depth = 0
        if anchor_pk is None or pk != anchor_pk:
            while True:
                p = parent_by_child.get(cur)
                if p is None:
                    break
                path_pks.append(p)
                depth += 1
                if anchor_pk is not None and p == anchor_pk:
                    break
                cur = p
                # Safety: don't loop forever on malformed data.
                if depth > 64:
                    break
        # Reverse so path goes from anchor → matched
        path_pks.reverse()
        path = []
        for ppk in path_pks:
            pn = nodes_by_pk.get(ppk)
            if pn is not None:
                path.append(pn.user.member_id)

        rows.append(
            {
                "member_id": mid,
                "full_name": name,
                "depth_from_anchor": depth,
                "path": path,
            }
        )

    rows.sort(key=lambda r: (r["depth_from_anchor"], r["member_id"] or ""))
    total = len(rows)
    if limit > 0 and total > limit:
        rows = rows[:limit]
    return rows, total


def _pending_binary_placement_orders():
    return Order.objects.filter(
        placement_status__in=(Order.PlacementStatus.PENDING, Order.PlacementStatus.FAILED),
        status=Order.Status.PAID,
        is_retail_purchase=False,
    ).exclude(Q(user__role=User.Role.SUPER_ADMIN) | Q(user__is_superuser=True))


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

    pending_orders = _pending_binary_placement_orders()
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
    qs = _pending_binary_placement_orders().select_related("user", "user__sponsor")

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
        time_remaining: str | None = None
        if o.placement_deadline_at:
            seconds_remaining = max(
                0.0, (o.placement_deadline_at - now).total_seconds()
            )
            if seconds_remaining >= 3600:
                hours = round(seconds_remaining / 3600.0, 3)
                time_remaining = f"{hours:g} hour{'s' if hours != 1 else ''}"
            else:
                minutes = int(seconds_remaining // 60)
                time_remaining = f"{minutes} minute{'s' if minutes != 1 else ''}"
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
                "time_remaining_to_auto_place": time_remaining,
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

    # Optional in-subtree search ("search within results"). Matches are
    # computed in Python over the already-loaded subtree, so this adds no DB
    # round-trips. Empty/whitespace q disables the feature.
    q_raw = request.query_params.get("q") or ""
    q = q_raw.strip()
    match_limit = _parse_int(
        request.query_params.get("match_limit"),
        default=ADMIN_BINARY_TREE_SEARCH_LIMIT_DEFAULT,
        lo=1,
        hi=ADMIN_BINARY_TREE_SEARCH_LIMIT_MAX,
    )

    meta = {
        "depth_requested": depth_requested,
        "depth_effective": depth_effective,
        "depth_max": ADMIN_BINARY_TREE_DEPTH_MAX,
        "depth_capped": depth_capped,
    }

    def _attach_search_block(
        payload: dict[str, Any],
        nodes_by_pk: dict[int, BinaryNode],
        root_pk: int | None,
    ) -> dict[str, Any]:
        """Compute matches over the loaded subtree, stamp is_match on the
        nested tree, and return the payload with a new ``search`` key."""
        if not q:
            return payload
        q_lower = q.lower()
        matched_pks = _find_matched_pks(nodes_by_pk, q_lower)
        matched_member_ids = {
            nodes_by_pk[pk].user.member_id
            for pk in matched_pks
            if pk in nodes_by_pk and nodes_by_pk[pk].user is not None
        }
        # Stamp is_match on every node in the nested payload (root may live
        # under "root" for the anchor case, or be the payload itself for
        # per-root entries in the no-anchor case).
        target = payload.get("root") if isinstance(payload.get("root"), dict) else payload
        _stamp_is_match(target, matched_member_ids)

        matches, total = _build_search_matches(
            matched_pks, nodes_by_pk, root_pk, match_limit
        )
        payload["search"] = {
            "q": q,
            "mode": "highlight",
            "match_count": total,
            "limit": match_limit,
            "truncated": total > len(matches),
            "matches": matches,
        }
        return payload

    if anchor_member_id:
        anchor = User.objects.filter(member_id__iexact=anchor_member_id).first()
        if not anchor:
            return envelope_response(None, message="Anchor not found", success=False, status=404)
        payload, nodes_by_pk, anchor_pk = team_services.nested_tree_at_anchor_user_loaded(
            anchor, depth_effective
        )
        if isinstance(payload, dict):
            payload = _attach_search_block(payload, nodes_by_pk, anchor_pk)
            payload = {**payload, "tree_query": meta}
        return envelope_response(payload)

    roots = (
        BinaryNode.objects.filter(parent__isnull=True)
        .select_related("user")
        .order_by("id")[:50]
    )
    data: list[dict[str, Any]] = []
    aggregate_match_total = 0
    aggregate_matches: list[dict[str, Any]] = []
    for r in roots:
        item, nodes_by_pk, root_pk = team_services.nested_tree_at_anchor_user_loaded(
            r.user, depth_effective
        )
        if isinstance(item, dict):
            item = _attach_search_block(item, nodes_by_pk, root_pk)
            item["tree_query"] = meta
            if q and isinstance(item.get("search"), dict):
                aggregate_match_total += int(item["search"].get("match_count") or 0)
                aggregate_matches.extend(item["search"].get("matches") or [])
        data.append(item)

    response: dict[str, Any] = {
        "roots": data,
        "depth": depth_effective,
        "tree_query": meta,
    }
    if q:
        # Provide a top-level rollup so the client doesn't have to merge
        # per-root search blocks itself. Cap the rolled-up matches list at
        # match_limit (already sorted by depth then member_id within each
        # root; we re-sort the merged list to keep ordering deterministic).
        aggregate_matches.sort(
            key=lambda r: (r.get("depth_from_anchor") or 0, r.get("member_id") or "")
        )
        truncated = len(aggregate_matches) > match_limit
        if truncated:
            aggregate_matches = aggregate_matches[:match_limit]
        response["search"] = {
            "q": q,
            "mode": "highlight",
            "match_count": aggregate_match_total,
            "limit": match_limit,
            "truncated": truncated or aggregate_match_total > len(aggregate_matches),
            "matches": aggregate_matches,
        }
    return envelope_response(response)


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

    pending_placements = _pending_binary_placement_orders().count()

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
    if not BinaryNode.objects.filter(user_id=parent.pk).exists():
        return envelope_response(
            None,
            message="Selected parent is not placed in the binary tree.",
            success=False,
            status=400,
        )
    if is_account_capped(parent):
        return envelope_response(
            None,
            message="Parent account has reached the earning cap and cannot host new placements",
            success=False,
            status=400,
        )

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

