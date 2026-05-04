from django.db.models import Exists, OuterRef, Sum
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request

from apps.common.permissions import IsAdminRole
from apps.common.responses import envelope_response
from apps.commissions.models import CommissionLedger
from apps.mlm_tree.models import BinaryNode
from apps.mlm_tree.placement import get_pending_placement_order
from apps.payments.models import Order

from . import team_services
from .models import User


def _truthy_query_param(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in ("1", "true", "yes", "on")


def _parse_int(q: str | None, default: int, *, min_v: int = 1, max_v: int | None = None) -> int:
    try:
        v = int(q or default)
    except (TypeError, ValueError):
        v = default
    v = max(min_v, v)
    if max_v is not None:
        v = min(v, max_v)
    return v


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def referral_me(request: Request):
    u = request.user
    return envelope_response(
        {
            "referral_code": u.referral_code,
            "referral_link": u.referral_link,
            "qr_url": u.referral_link,
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def referral_stats(request: Request):
    u = request.user
    qs = u.commissions_received.filter(commission_type=CommissionLedger.CommissionType.DIRECT)
    total = qs.aggregate(s=Sum("net_amount"))["s"] or 0
    return envelope_response(
        {"direct_referrals": u.direct_referrals.count(), "direct_commission_total": str(total)}
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def referral_list(request: Request):
    qs = request.user.direct_referrals.all().order_by("-id")
    pending = _truthy_query_param(request.query_params.get("pending_placement"))
    if pending:
        mlm_paid_order = Order.objects.filter(
            user_id=OuterRef("pk"),
            status=Order.Status.PAID,
            is_retail_purchase=False,
        )
        qs = qs.filter(Exists(mlm_paid_order)).filter(binary_node__isnull=True)
    rows = list(qs[:50])
    data = []
    for x in rows:
        row = {
            "member_id": x.member_id,
            "full_name": x.full_name,
            "created_at": x.created_at.isoformat(),
        }
        if pending:
            po = get_pending_placement_order(x)
            if po:
                row["placement_deadline_at"] = (
                    po.placement_deadline_at.isoformat() if po.placement_deadline_at else None
                )
                row["placement_status"] = po.placement_status
                row["placement_order_id"] = po.id
        data.append(row)
    payload: dict = {"results": data, "count": qs.count()}
    if pending:
        payload["pending_placement"] = True
        ctx = team_services.build_subtree_context(request.user)
        payload["viewer_leg_counts"] = {"left": ctx.left_leg_count, "right": ctx.right_leg_count}
        payload["suggested_leg"] = team_services.suggested_leg(
            ctx.left_leg_count, ctx.right_leg_count
        )
    return envelope_response(payload)


def _parse_max_depth(request: Request, default: int = 3, cap: int = 10) -> int:
    raw = request.query_params.get("max_depth", str(default))
    try:
        v = int(raw)
    except (TypeError, ValueError):
        v = default
    return max(0, min(v, cap))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def team_network(request: Request):
    """Bundled My Network payload: summary, pending, tree, optional roster (see include)."""
    viewer = request.user
    ctx = team_services.build_subtree_context(viewer)
    include = team_services.parse_include(request.query_params.get("include"))
    max_depth = team_services.parse_max_depth(
        request.query_params.get("tree_max_depth"),
        default=3,
        cap=10,
    )
    anchor_raw = (request.query_params.get("anchor_member_id") or "").strip()
    if not anchor_raw or anchor_raw.upper() == (viewer.member_id or "").upper():
        anchor_user = viewer
    else:
        anchor_user = User.objects.filter(member_id__iexact=anchor_raw).first()
        if not anchor_user:
            return envelope_response(None, message="Member not found", success=False, status=404)

    data: dict = {}
    if "summary" in include:
        data["summary"] = team_services.build_summary(viewer, ctx)
    if "pending" in include:
        data["pending"] = team_services.build_pending_payload(viewer, ctx)
    if "tree" in include:
        if not ctx.viewer_node:
            if anchor_user.id != viewer.id:
                return envelope_response(
                    None,
                    message="You are not in the binary tree; cannot view other members' subtrees",
                    success=False,
                    status=403,
                )
            data["tree"] = team_services.nested_tree_at_anchor_user(anchor_user, max_depth)
        else:
            if anchor_user.id not in ctx.subtree_user_ids:
                return envelope_response(
                    None,
                    message="You can only view subtrees for yourself or members below you in your binary leg",
                    success=False,
                    status=403,
                )
            data["tree"] = team_services.build_tree_section(ctx, anchor_user, max_depth)
    if "roster" in include:
        page = _parse_int(request.query_params.get("roster_page"), 1, min_v=1, max_v=10_000)
        page_size = _parse_int(request.query_params.get("roster_page_size"), 20, min_v=1, max_v=100)
        leg = (request.query_params.get("leg") or "").strip().lower() or None
        if leg not in (None, "", "left", "right"):
            leg = None
        kyc = (request.query_params.get("kyc") or "").strip().lower() or None
        if kyc not in (None, "", "pending", "verified"):
            kyc = None
        activity = (request.query_params.get("activity") or "").strip().lower() or None
        if activity not in (None, "", "active", "inactive"):
            activity = None
        search = request.query_params.get("search")
        data["roster"] = team_services.build_roster_slice(
            viewer,
            ctx,
            page=page,
            page_size=page_size,
            leg=leg,
            kyc=kyc,
            activity=activity,
            search=search,
        )
    return envelope_response(data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def team_network_roster(request: Request):
    viewer = request.user
    ctx = team_services.build_subtree_context(viewer)
    page = _parse_int(request.query_params.get("page"), 1, min_v=1, max_v=10_000)
    page_size = _parse_int(request.query_params.get("page_size"), 20, min_v=1, max_v=100)
    leg = (request.query_params.get("leg") or "").strip().lower() or None
    if leg not in (None, "", "left", "right"):
        leg = None
    kyc = (request.query_params.get("kyc") or "").strip().lower() or None
    if kyc not in (None, "", "pending", "verified"):
        kyc = None
    activity = (request.query_params.get("activity") or "").strip().lower() or None
    if activity not in (None, "", "active", "inactive"):
        activity = None
    search = request.query_params.get("search")
    payload = team_services.build_roster_slice(
        viewer,
        ctx,
        page=page,
        page_size=page_size,
        leg=leg,
        kyc=kyc,
        activity=activity,
        search=search,
    )
    return envelope_response(payload)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def tree_subtree(request: Request):
    """Nested binary subtree under an anchor member (self or any node below you in your leg)."""
    viewer = request.user
    anchor_raw = (request.query_params.get("anchor_member_id") or "").strip()
    max_depth = _parse_max_depth(request)

    if not anchor_raw or anchor_raw.upper() == (viewer.member_id or "").upper():
        anchor_user = viewer
    else:
        anchor_user = User.objects.filter(member_id__iexact=anchor_raw).first()
        if not anchor_user:
            return envelope_response(None, message="Member not found", success=False, status=404)

    allowed_ids = team_services.binary_subtree_user_ids_from_viewer(viewer)
    if allowed_ids is None:
        if anchor_user.id != viewer.id:
            return envelope_response(
                None,
                message="You are not in the binary tree; cannot view other members' subtrees",
                success=False,
                status=403,
            )
        body = team_services.nested_tree_at_anchor_user(anchor_user, max_depth)
        return envelope_response(
            body,
            message="You are not placed in the binary tree",
        )

    if anchor_user.id not in allowed_ids:
        return envelope_response(
            None,
            message="You can only view subtrees for yourself or members below you in your binary leg",
            success=False,
            status=403,
        )

    body = team_services.nested_tree_at_anchor_user(anchor_user, max_depth)
    return envelope_response(body)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def tree_me(request: Request):
    u = request.user
    return envelope_response(team_services.nested_tree_at_anchor_user(u, 3))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def tree_uplines(request: Request):
    u = request.user
    n = BinaryNode.objects.filter(user_id=u.pk).select_related("user").first()
    if not n:
        return envelope_response({"uplines": []})
    chain = []
    cur = n.parent
    while cur and len(chain) < 4:
        chain.append(
            {
                "member_id": cur.user.member_id,
                "full_name": cur.user.full_name,
                "level": cur.level,
            }
        )
        cur = cur.parent
    return envelope_response({"uplines": chain})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def tree_downlines(request: Request):
    u = request.user
    root = BinaryNode.objects.filter(user_id=u.pk).first()
    if not root:
        return envelope_response({"results": [], "count": 0})
    out = []
    q = [root]
    while q:
        n = q.pop(0)
        for child in (n.left_child, n.right_child):
            if child:
                out.append(
                    {
                        "member_id": child.user.member_id,
                        "full_name": child.user.full_name,
                        "level": child.level,
                    }
                )
                q.append(child)
    return envelope_response({"results": out, "count": len(out)})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def tree_level_n(request: Request, n: int):
    return envelope_response({"level": n, "members": []})


@api_view(["GET"])
@permission_classes([IsAdminRole])
def admin_tree_user(request: Request, user_id: int):
    try:
        target = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return envelope_response(None, message="Not found", success=False, status=404)
    return envelope_response(team_services.nested_tree_at_anchor_user(target, 10))


@api_view(["GET"])
@permission_classes([IsAdminRole])
def admin_tree_platform(request: Request):
    roots = BinaryNode.objects.filter(parent__isnull=True).select_related("user")[:20]
    data = [team_services.nested_tree_at_anchor_user(r.user, 2) for r in roots]
    return envelope_response({"roots": data})
