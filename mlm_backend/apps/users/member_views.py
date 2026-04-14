from django.db.models import Sum
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request

from apps.common.permissions import IsAdminRole
from apps.common.responses import envelope_response
from apps.commissions.models import CommissionLedger
from apps.mlm_tree.models import BinaryNode

from .models import User


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
    data = [
        {"member_id": x.member_id, "full_name": x.full_name, "created_at": x.created_at.isoformat()}
        for x in qs[:50]
    ]
    return envelope_response({"results": data, "count": qs.count()})


def _tree_node(node: BinaryNode | None, depth: int, max_depth: int):
    if node is None or depth > max_depth:
        return None
    return {
        "member_id": node.user.member_id,
        "full_name": node.user.full_name,
        "position": node.position,
        "level": node.level,
        "left": _tree_node(node.left_child, depth + 1, max_depth),
        "right": _tree_node(node.right_child, depth + 1, max_depth),
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def tree_me(request: Request):
    u = request.user
    if not hasattr(u, "binary_node"):
        return envelope_response({"root": None})
    root = u.binary_node
    return envelope_response({"root": _tree_node(root, 0, 3)})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def tree_uplines(request: Request):
    u = request.user
    if not hasattr(u, "binary_node"):
        return envelope_response({"uplines": []})
    n = u.binary_node
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
    if not hasattr(u, "binary_node"):
        return envelope_response({"results": [], "count": 0})
    # simplified: BFS list ids
    out = []
    q = [u.binary_node]
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
    if not hasattr(target, "binary_node"):
        return envelope_response({"root": None})
    return envelope_response({"root": _tree_node(target.binary_node, 0, 10)})


@api_view(["GET"])
@permission_classes([IsAdminRole])
def admin_tree_platform(request: Request):
    roots = BinaryNode.objects.filter(parent__isnull=True)
    data = [_tree_node(r, 0, 2) for r in roots[:20]]
    return envelope_response({"roots": data})
