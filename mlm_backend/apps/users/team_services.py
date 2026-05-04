"""Team / binary network: subtree graph, summaries, roster — batched queries (no N+1)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Exists, OuterRef, Sum
from django.utils import timezone

from apps.commissions.models import CommissionLedger
from apps.mlm_tree.models import BinaryNode
from apps.payments.models import Order

from .models import User


def initials_from_full_name(full_name: str) -> str:
    parts = (full_name or "").strip().split()
    if not parts:
        return "?"
    if len(parts) == 1:
        return (parts[0][:2] or "?").upper()
    return (parts[0][0] + parts[1][0]).upper()


def user_node_public_fields(user: User) -> dict[str, Any]:
    return {
        "member_id": user.member_id,
        "full_name": user.full_name,
        "initials": initials_from_full_name(user.full_name),
        "kyc_status": user.kyc_status,
        "account_status": user.account_status,
        "is_active": user.is_active,
        "rank_label": None,
    }


def collect_subtree_node_pks(root_pk: int) -> set[int]:
    """BFS by frontier; O(depth) batched queries, not one per node."""
    found: set[int] = {root_pk}
    frontier: set[int] = {root_pk}
    while frontier:
        rows = BinaryNode.objects.filter(pk__in=frontier).values_list(
            "pk", "left_child_id", "right_child_id"
        )
        next_frontier: set[int] = set()
        for pk, lid, rid in rows:
            for cid in (lid, rid):
                if cid and cid not in found:
                    found.add(cid)
                    next_frontier.add(cid)
        frontier = next_frontier
    return found


def collect_subtree_node_pks_limited(root_pk: int, max_depth: int) -> set[int]:
    """Node PKs within max_depth rings below root (root at depth 0)."""
    found: set[int] = {root_pk}
    depth_by_pk: dict[int, int] = {root_pk: 0}
    frontier: set[int] = {root_pk}
    while frontier:
        rows = BinaryNode.objects.filter(pk__in=frontier).values_list(
            "pk", "left_child_id", "right_child_id"
        )
        next_f: set[int] = set()
        for pk, lid, rid in rows:
            d = depth_by_pk[pk]
            if d >= max_depth:
                continue
            for cid in (lid, rid):
                if cid and cid not in found:
                    found.add(cid)
                    depth_by_pk[cid] = d + 1
                    next_f.add(cid)
        frontier = next_f
    return found


def load_nodes_with_users(pks: set[int]) -> dict[int, BinaryNode]:
    if not pks:
        return {}
    nodes = (
        BinaryNode.objects.filter(pk__in=pks)
        .select_related("user")
        .select_related("left_child", "right_child")
    )
    return {n.pk: n for n in nodes}


def build_parent_by_child(nodes_by_pk: dict[int, BinaryNode]) -> dict[int, int]:
    parent: dict[int, int] = {}
    for n in nodes_by_pk.values():
        if n.left_child_id:
            parent[n.left_child_id] = n.pk
        if n.right_child_id:
            parent[n.right_child_id] = n.pk
    return parent


def build_user_id_to_node_pk(nodes_by_pk: dict[int, BinaryNode]) -> dict[int, int]:
    return {n.user_id: n.pk for n in nodes_by_pk.values()}


def count_subtree_nodes_from_child(
    nodes_by_pk: dict[int, BinaryNode], child_root_pk: int | None
) -> int:
    if not child_root_pk or child_root_pk not in nodes_by_pk:
        return 0
    c = 0
    q: deque[int] = deque([child_root_pk])
    seen: set[int] = set()
    while q:
        pk = q.popleft()
        if pk in seen:
            continue
        seen.add(pk)
        c += 1
        n = nodes_by_pk.get(pk)
        if not n:
            continue
        if n.left_child_id:
            q.append(n.left_child_id)
        if n.right_child_id:
            q.append(n.right_child_id)
    return c


def leg_from_viewer(
    nodes_by_pk: dict[int, BinaryNode],
    parent_by_child: dict[int, int],
    viewer_node_pk: int,
    target_user_id: int,
    viewer_user_id: int,
) -> str | None:
    if target_user_id == viewer_user_id:
        return None
    uid_to_pk = build_user_id_to_node_pk(nodes_by_pk)
    cur = uid_to_pk.get(target_user_id)
    if cur is None:
        return None
    while cur != viewer_node_pk:
        p = parent_by_child.get(cur)
        if p is None:
            return None
        if p == viewer_node_pk:
            vn = nodes_by_pk[viewer_node_pk]
            if vn.left_child_id == cur:
                return BinaryNode.Position.LEFT
            if vn.right_child_id == cur:
                return BinaryNode.Position.RIGHT
            return None
        cur = p
    return None


def depth_from_viewer(
    nodes_by_pk: dict[int, BinaryNode],
    viewer_node_pk: int,
    target_user_id: int,
    viewer_user_id: int,
) -> int | None:
    if target_user_id == viewer_user_id:
        return 0
    uid_to_pk = build_user_id_to_node_pk(nodes_by_pk)
    cur = uid_to_pk.get(target_user_id)
    if cur is None:
        return None
    d = 0
    parent_by_child = build_parent_by_child(nodes_by_pk)
    while cur != viewer_node_pk:
        p = parent_by_child.get(cur)
        if p is None:
            return None
        d += 1
        cur = p
    return d


def build_tree_nested(
    anchor_pk: int,
    max_depth: int,
    nodes_by_pk: dict[int, BinaryNode],
    depth_from_anchor: int,
) -> dict[str, Any] | None:
    if anchor_pk not in nodes_by_pk or depth_from_anchor > max_depth:
        return None
    node = nodes_by_pk[anchor_pk]
    u = node.user
    payload: dict[str, Any] = {
        **user_node_public_fields(u),
        "position": node.position,
        "level": node.level,
    }
    if depth_from_anchor < max_depth:
        left_pk = node.left_child_id
        right_pk = node.right_child_id
        payload["left"] = (
            build_tree_nested(left_pk, max_depth, nodes_by_pk, depth_from_anchor + 1)
            if left_pk
            else None
        )
        payload["right"] = (
            build_tree_nested(right_pk, max_depth, nodes_by_pk, depth_from_anchor + 1)
            if right_pk
            else None
        )
    else:
        payload["left"] = None
        payload["right"] = None
    return payload


def parse_include(raw: str | None) -> set[str]:
    if not raw or not raw.strip():
        return {"summary", "pending", "tree"}
    parts = {p.strip().lower() for p in raw.split(",") if p.strip()}
    allowed = {"summary", "pending", "tree", "roster"}
    return parts & allowed if parts else {"summary", "pending", "tree"}


def parse_max_depth(raw: str | None, default: int = 3, cap: int = 10) -> int:
    try:
        v = int(raw or default)
    except (TypeError, ValueError):
        v = default
    return max(0, min(v, cap))


def suggested_leg(left: int, right: int) -> str:
    if left < right:
        return BinaryNode.Position.LEFT
    if right < left:
        return BinaryNode.Position.RIGHT
    return BinaryNode.Position.RIGHT


def rolling_week_start() -> timezone.datetime:
    return timezone.now() - timedelta(days=7)


@dataclass
class SubtreeContext:
    viewer_node: BinaryNode | None
    nodes_by_pk: dict[int, BinaryNode]
    subtree_user_ids: set[int]
    parent_by_child: dict[int, int]
    left_leg_count: int
    right_leg_count: int


def binary_subtree_user_ids_from_viewer(viewer: User) -> set[int] | None:
    """User ids in viewer's binary subtree (including viewer). None if viewer has no node."""
    vn = BinaryNode.objects.filter(user_id=viewer.pk).first()
    if not vn:
        return None
    pks = collect_subtree_node_pks(vn.pk)
    nodes = load_nodes_with_users(pks)
    return {n.user_id for n in nodes.values()}


def nested_tree_at_anchor_user(anchor_user: User, max_depth: int) -> dict[str, Any]:
    """Depth-limited nested tree for public tree endpoints (batched load, no N+1)."""
    node = BinaryNode.objects.filter(user_id=anchor_user.pk).first()
    if not node:
        return {
            "root": None,
            "anchor_member_id": anchor_user.member_id,
            "max_depth": max_depth,
        }
    pks = collect_subtree_node_pks_limited(node.pk, max_depth)
    nodes = load_nodes_with_users(pks)
    root = build_tree_nested(node.pk, max_depth, nodes, 0)
    return {
        "root": root,
        "anchor_member_id": anchor_user.member_id,
        "max_depth": max_depth,
    }


def build_subtree_context(viewer: User) -> SubtreeContext:
    vn = BinaryNode.objects.filter(user_id=viewer.pk).select_related("user").first()
    if not vn:
        return SubtreeContext(
            viewer_node=None,
            nodes_by_pk={},
            subtree_user_ids={viewer.pk},
            parent_by_child={},
            left_leg_count=0,
            right_leg_count=0,
        )
    pks = collect_subtree_node_pks(vn.pk)
    nodes_by_pk = load_nodes_with_users(pks)
    vn2 = nodes_by_pk.get(vn.pk)
    if not vn2:
        return SubtreeContext(
            viewer_node=None,
            nodes_by_pk={},
            subtree_user_ids={viewer.pk},
            parent_by_child={},
            left_leg_count=0,
            right_leg_count=0,
        )
    parent_by_child = build_parent_by_child(nodes_by_pk)
    subtree_user_ids = {n.user_id for n in nodes_by_pk.values()}
    left_c = count_subtree_nodes_from_child(nodes_by_pk, vn2.left_child_id)
    right_c = count_subtree_nodes_from_child(nodes_by_pk, vn2.right_child_id)
    return SubtreeContext(
        viewer_node=vn2,
        nodes_by_pk=nodes_by_pk,
        subtree_user_ids=subtree_user_ids,
        parent_by_child=parent_by_child,
        left_leg_count=left_c,
        right_leg_count=right_c,
    )


def build_summary(viewer: User, ctx: SubtreeContext) -> dict[str, Any]:
    week_start = rolling_week_start()
    direct_qs = viewer.direct_referrals.all()
    total_refs = direct_qs.count()
    refs_week = direct_qs.filter(created_at__gte=week_start).count()

    mlm_paid = Order.objects.filter(
        user_id=OuterRef("pk"),
        status=Order.Status.PAID,
        is_retail_purchase=False,
    )
    pending_qs = (
        direct_qs.filter(Exists(mlm_paid))
        .filter(binary_node__isnull=True)
        .order_by("-id")
    )
    pending_count = pending_qs.count()

    active_placed = 0
    if ctx.subtree_user_ids:
        ex = ctx.subtree_user_ids - {viewer.pk}
        if ex:
            active_placed = User.objects.filter(
                pk__in=ex,
                kyc_status=User.KYCStatus.VERIFIED,
                account_status=User.AccountStatus.ACTIVE,
            ).count()

    left_c = ctx.left_leg_count
    right_c = ctx.right_leg_count
    weaker = suggested_leg(left_c, right_c)
    if left_c > right_c:
        left_lbl, right_lbl = "strong", "weak"
    elif right_c > left_c:
        left_lbl, right_lbl = "weak", "strong"
    else:
        left_lbl, right_lbl = "neutral", "neutral"
    return {
        "total_referrals": total_refs,
        "referrals_last_7_days": refs_week,
        "pending_placement_count": pending_count,
        "active_placed_kyc_verified_count": active_placed,
        "left_leg_count": left_c,
        "right_leg_count": right_c,
        "weaker_leg": weaker,
        "left_leg_label": left_lbl,
        "right_leg_label": right_lbl,
        "subtree_member_count": max(0, len(ctx.subtree_user_ids) - 1),
    }


def pending_direct_referrals_queryset(viewer: User):
    mlm_paid = Order.objects.filter(
        user_id=OuterRef("pk"),
        status=Order.Status.PAID,
        is_retail_purchase=False,
    )
    return (
        viewer.direct_referrals.filter(Exists(mlm_paid))
        .filter(binary_node__isnull=True)
        .order_by("-id")
    )


def build_pending_payload(viewer: User, ctx: SubtreeContext, cap: int = 50) -> dict[str, Any]:
    qs = pending_direct_referrals_queryset(viewer)
    count = qs.count()
    users = list(qs[:cap])
    user_ids = [u.pk for u in users]
    orders_by_user: dict[int, Order] = {}
    if user_ids:
        oqs = (
            Order.objects.filter(
                user_id__in=user_ids,
                status=Order.Status.PAID,
                is_retail_purchase=False,
                placement_status__in=(
                    Order.PlacementStatus.PENDING,
                    Order.PlacementStatus.FAILED,
                ),
            )
            .order_by("user_id", "id")
        )
        for o in oqs:
            if o.user_id not in orders_by_user:
                orders_by_user[o.user_id] = o

    left_c = ctx.left_leg_count
    right_c = ctx.right_leg_count
    sug = suggested_leg(left_c, right_c)
    now = timezone.now()

    results = []
    for u in users:
        row: dict[str, Any] = {
            "member_id": u.member_id,
            "full_name": u.full_name,
            "created_at": u.created_at.isoformat(),
        }
        po = orders_by_user.get(u.pk)
        if po:
            row["placement_deadline_at"] = (
                po.placement_deadline_at.isoformat() if po.placement_deadline_at else None
            )
            row["placement_status"] = po.placement_status
            row["placement_order_id"] = po.id
            if po.placement_deadline_at:
                row["hours_remaining"] = max(
                    0.0,
                    (po.placement_deadline_at - now).total_seconds() / 3600.0,
                )
            else:
                row["hours_remaining"] = None
        else:
            row["placement_deadline_at"] = None
            row["placement_status"] = None
            row["placement_order_id"] = None
            row["hours_remaining"] = None
        results.append(row)

    return {
        "results": results,
        "count": count,
        "viewer_leg_counts": {"left": left_c, "right": right_c},
        "suggested_leg": sug,
    }


def compute_roster_filter_totals(
    roster_ids: list[int],
    leg_by_user: dict[int, str | None],
    users_meta: dict[int, dict[str, Any]],
) -> dict[str, int]:
    """Single pass over precomputed roster members (no extra DB)."""
    totals = {
        "all": len(roster_ids),
        "left_leg": 0,
        "right_leg": 0,
        "active": 0,
        "kyc_pending": 0,
    }
    for uid in roster_ids:
        leg = leg_by_user.get(uid)
        if leg == BinaryNode.Position.LEFT:
            totals["left_leg"] += 1
        elif leg == BinaryNode.Position.RIGHT:
            totals["right_leg"] += 1
        meta = users_meta.get(uid) or {}
        if meta.get("is_active") and meta.get("account_status") == User.AccountStatus.ACTIVE:
            totals["active"] += 1
        if meta.get("kyc_status") == User.KYCStatus.PENDING:
            totals["kyc_pending"] += 1
    return totals


def build_roster_slice(
    viewer: User,
    ctx: SubtreeContext | None,
    *,
    page: int,
    page_size: int,
    leg: str | None,
    kyc: str | None,
    activity: str | None,
    search: str | None,
    page_cap: int = 100,
) -> dict[str, Any]:
    if not ctx or not ctx.viewer_node:
        return {
            "results": [],
            "page": page,
            "page_size": page_size,
            "count": 0,
            "filter_totals": {
                "all": 0,
                "left_leg": 0,
                "right_leg": 0,
                "active": 0,
                "kyc_pending": 0,
            },
        }

    viewer_node_pk = ctx.viewer_node.pk
    ex_ids = ctx.subtree_user_ids - {viewer.pk}
    if not ex_ids:
        z = {"all": 0, "left_leg": 0, "right_leg": 0, "active": 0, "kyc_pending": 0}
        return {
            "results": [],
            "page": page,
            "page_size": page_size,
            "count": 0,
            "filter_totals": z,
        }

    base_users = User.objects.filter(pk__in=ex_ids).values(
        "id",
        "member_id",
        "full_name",
        "created_at",
        "kyc_status",
        "account_status",
        "is_active",
        "sponsor_id",
    )
    users_meta: dict[int, dict[str, Any]] = {row["id"]: dict(row) for row in base_users}

    leg_by_user: dict[int, str | None] = {}
    for uid in ex_ids:
        leg_by_user[uid] = leg_from_viewer(
            ctx.nodes_by_pk,
            ctx.parent_by_child,
            viewer_node_pk,
            uid,
            viewer.pk,
        )

    def meta_match(uid: int) -> bool:
        m = users_meta.get(uid)
        if not m:
            return False
        if kyc == "pending" and m["kyc_status"] != User.KYCStatus.PENDING:
            return False
        if kyc == "verified" and m["kyc_status"] != User.KYCStatus.VERIFIED:
            return False
        if activity == "active" and (
            not m["is_active"] or m["account_status"] != User.AccountStatus.ACTIVE
        ):
            return False
        if activity == "inactive":
            if m["is_active"] and m["account_status"] == User.AccountStatus.ACTIVE:
                return False
        if search and search.strip():
            s = search.strip().lower()
            if s not in (m["full_name"] or "").lower() and s not in (
                m["member_id"] or ""
            ).lower():
                return False
        return True

    roster_ids = [uid for uid in ex_ids if meta_match(uid)]

    if leg and leg.upper() in ("LEFT", "RIGHT"):
        want = leg.upper()
        roster_ids = [uid for uid in roster_ids if leg_by_user.get(uid) == want]

    filter_totals = compute_roster_filter_totals(list(ex_ids), leg_by_user, users_meta)

    roster_ids.sort(key=lambda uid: users_meta[uid]["created_at"], reverse=True)
    count = len(roster_ids)
    page = max(1, page)
    page_size = max(1, min(page_size, page_cap))
    start = (page - 1) * page_size
    page_ids = roster_ids[start : start + page_size]

    earnings: dict[int, Decimal] = {}
    if page_ids:
        agg = (
            CommissionLedger.objects.filter(
                recipient_id=viewer.pk,
                source_user_id__in=page_ids,
            )
            .exclude(status=CommissionLedger.Status.REVERSED)
            .values("source_user_id")
            .annotate(total=Sum("net_amount"))
        )
        for row in agg:
            earnings[row["source_user_id"]] = row["total"] or Decimal("0")

    results = []
    for uid in page_ids:
        m = users_meta[uid]
        depth = depth_from_viewer(ctx.nodes_by_pk, viewer_node_pk, uid, viewer.pk)
        if m.get("sponsor_id") == viewer.pk:
            lv_label = "Direct"
        elif depth is not None and depth >= 1:
            lv_label = f"L{depth}"
        else:
            lv_label = "Team"
        results.append(
            {
                "member_id": m["member_id"],
                "full_name": m["full_name"],
                "initials": initials_from_full_name(m["full_name"] or ""),
                "joined_at": m["created_at"].isoformat() if m["created_at"] else None,
                "binary_level_from_viewer": depth,
                "level_label": lv_label,
                "is_direct_referral": m.get("sponsor_id") == viewer.pk,
                "leg_from_viewer": leg_by_user.get(uid),
                "kyc_status": m["kyc_status"],
                "account_status": m["account_status"],
                "is_active": m["is_active"],
                "your_earning_total": str(earnings.get(uid, Decimal("0"))),
            }
        )

    return {
        "results": results,
        "page": page,
        "page_size": page_size,
        "count": count,
        "filter_totals": filter_totals,
    }


def build_tree_section(
    ctx: SubtreeContext,
    anchor_user: User,
    max_depth: int,
) -> dict[str, Any]:
    if not ctx.viewer_node:
        return {
            "root": None,
            "anchor_member_id": anchor_user.member_id,
            "max_depth": max_depth,
        }
    if anchor_user.id not in ctx.subtree_user_ids:
        return {
            "root": None,
            "anchor_member_id": anchor_user.member_id,
            "max_depth": max_depth,
        }
    uid_to_pk = build_user_id_to_node_pk(ctx.nodes_by_pk)
    anchor_pk = uid_to_pk.get(anchor_user.pk)
    if anchor_pk is None:
        return {
            "root": None,
            "anchor_member_id": anchor_user.member_id,
            "max_depth": max_depth,
        }
    pks = collect_subtree_node_pks_limited(anchor_pk, max_depth)
    nodes = load_nodes_with_users(pks)
    root = build_tree_nested(anchor_pk, max_depth, nodes, 0)
    return {
        "root": root,
        "anchor_member_id": anchor_user.member_id,
        "max_depth": max_depth,
    }
