from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from django.db.models import Exists, OuterRef, Sum
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request

from apps.common.permissions import IsAdminRole, require_kyc_verified_and_compliant
from apps.common.responses import envelope_response
from apps.commissions.milestone_tiers import MILESTONES
from apps.commissions.models import CommissionLedger
from apps.mlm_tree.models import BinaryNode
from apps.mlm_tree.placement import get_pending_placement_order
from apps.payments.models import Order
from apps.wallet.models import WalletTransaction, WithdrawalRequest
from apps.wallet.services.member_money import (
    build_earnings_response,
    build_payouts_bundle,
    build_todays_earnings_for_dashboard,
)

from . import team_services
from .models import User


def _iso(dt: datetime) -> str:
    if timezone.is_aware(dt):
        return dt.isoformat()
    if timezone.is_naive(dt) and timezone.get_current_timezone():
        try:
            return timezone.make_aware(dt, timezone.get_current_timezone()).isoformat()
        except Exception:
            return dt.isoformat()
    return dt.isoformat()


def _fmt_money(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return str(v)
    return str(v)


def _activity_row(*, at: datetime, kind: str, title: str, subtitle: str | None, amount: Any, meta: dict):
    return {
        "kind": kind,
        "title": title,
        "subtitle": subtitle,
        "amount": _fmt_money(amount),
        "at": _iso(at),
        "meta": meta,
    }


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
    blocked = require_kyc_verified_and_compliant(request)
    if blocked is not None:
        return blocked
    qs = request.user.direct_referrals.all().order_by("-id")
    pending = _truthy_query_param(request.query_params.get("pending_placement"))
    if pending:
        mlm_paid_order = Order.objects.filter(
            user_id=OuterRef("pk"),
            status=Order.Status.PAID,
            is_retail_purchase=False,
        )
        qs = qs.filter(Exists(mlm_paid_order)).filter(binary_node__isnull=True)

    page = _parse_int(request.query_params.get("page"), 1, min_v=1, max_v=10_000)
    page_size = _parse_int(request.query_params.get("page_size"), 20, min_v=1, max_v=100)
    count = qs.count()
    total_pages = (count + page_size - 1) // page_size if count else 0
    start = (page - 1) * page_size
    rows = list(qs[start : start + page_size])

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
    payload: dict = {
        "results": data,
        "count": count,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }
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
    blocked = require_kyc_verified_and_compliant(request)
    if blocked is not None:
        return blocked
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
    blocked = require_kyc_verified_and_compliant(request)
    if blocked is not None:
        return blocked
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
@permission_classes([IsAuthenticated])
def user_dashboard(request: Request):
    """
    Member dashboard bundle: tiles + cap/breakdown + unified recent activity.
    """
    u: User = request.user

    blocked = require_kyc_verified_and_compliant(request)
    if blocked:
        return blocked

    earnings = build_earnings_response(
        u,
        include_raw="overview",
        period="all",
        ledger_type="all",
        page=1,
        page_size=1,
    )
    payouts = build_payouts_bundle(u, include_movements=True)

    direct_referrals = int(getattr(u, "direct_referral_count", 0) or 0)
    thresholds = [int(t[0]) for t in MILESTONES]
    milestones_total = len(thresholds)
    milestones_current = sum(1 for t in thresholds if direct_referrals >= t)
    next_target = next((t for t in thresholds if direct_referrals < t), None)

    summary = (earnings or {}).get("summary") or {}
    cap_block = summary.get("cap") or {}
    income = summary.get("income") or {}
    direct_l1 = income.get("direct_l1") or {}
    passive_l2_l4 = income.get("passive_l2_l4") or {}
    milestone_inc = income.get("milestone") or {}
    slots_inc = income.get("slots") or {}

    # Recent activity: merge-sort across bounded sources.
    feed_limit = 20
    activity: list[dict[str, Any]] = []

    # (1) Earnings / milestone ledger (commissions + milestones) via existing builder.
    ledger = build_earnings_response(
        u,
        include_raw="ledger",
        period="30d",
        ledger_type="all",
        page=1,
        page_size=10,
    ).get("ledger", {})
    for row in (ledger.get("rows") or [])[:10]:
        try:
            at = timezone.datetime.fromisoformat(row.get("at"))
        except Exception:
            at = timezone.now()
        activity.append(
            _activity_row(
                at=at,
                kind="EARNINGS",
                title=str(row.get("type") or "Earnings"),
                subtitle=str(row.get("detail") or row.get("description") or "") or None,
                amount=row.get("net") or row.get("net_credited"),
                meta={
                    "kind": row.get("kind") or row.get("entry_kind"),
                    "status": row.get("status"),
                    "order_id": row.get("order_id"),
                },
            )
        )

    # (2) Wallet movements
    for tx in WalletTransaction.objects.filter(user=u).order_by("-created_at").only(
        "tx_type", "amount", "reference", "created_at"
    )[:10]:
        # Avoid duplicating commission/milestone events that are already included via earnings ledger.
        # Wallet transactions for those events use deterministic references from commissions engine.
        ref = (tx.reference or "").strip()
        if ref.startswith(("COMM-", "MILESTONE-", "REV-")):
            continue
        title = "Wallet credit" if tx.tx_type == WalletTransaction.TxType.CREDIT else "Wallet debit"
        activity.append(
            _activity_row(
                at=tx.created_at,
                kind="WALLET",
                title=title,
                subtitle=ref or None,
                amount=tx.amount,
                meta={"tx_type": tx.tx_type, "reference": ref},
            )
        )

    # (3) Withdrawals status changes
    for wr in WithdrawalRequest.objects.filter(user=u).order_by("-updated_at").only(
        "id", "status", "net_payable", "updated_at", "created_at", "reject_reason"
    )[:10]:
        activity.append(
            _activity_row(
                at=wr.updated_at or wr.created_at,
                kind="WITHDRAWAL",
                title=f"Withdrawal {wr.status.lower()}",
                subtitle=(wr.reject_reason or None) if wr.status == WithdrawalRequest.Status.REJECTED else None,
                amount=wr.net_payable,
                meta={"withdrawal_id": wr.id, "status": wr.status},
            )
        )

    # (4) New direct referrals joined (most recent)
    for r in u.direct_referrals.all().order_by("-created_at").only("member_id", "full_name", "created_at")[:10]:
        activity.append(
            _activity_row(
                at=r.created_at,
                kind="REFERRAL",
                title="New referral joined",
                subtitle=f"{r.full_name} ({r.member_id})",
                amount=None,
                meta={"member_id": r.member_id, "full_name": r.full_name},
            )
        )

    activity.sort(key=lambda x: x.get("at") or "", reverse=True)
    activity = activity[:feed_limit]

    data = {
        "profile": {"full_name": u.full_name, "member_id": u.member_id},
        "tiles": {
            "total_earnings": summary.get("lifetime_earnings"),
            "direct_referrals": direct_referrals,
            "available_balance": ((payouts.get("wallet") or {}).get("available_balance")),
            "milestones": {"current": milestones_current, "total": milestones_total, "next_target": next_target},
            **build_todays_earnings_for_dashboard(u),
        },
        "earnings_cap": {
            "used_amount": cap_block.get("used"),
            "cap_amount": cap_block.get("limit"),
            "used_percent": cap_block.get("used_pct"),
            "remaining_amount": cap_block.get("remaining"),
        },
        "earnings_breakdown": {
            "direct": direct_l1.get("amount"),
            "passive": passive_l2_l4.get("amount"),
            "milestone": milestone_inc.get("amount"),
            "slots": slots_inc.get("amount"),
        },
        "wallet": payouts.get("wallet"),
        "recent_activity": activity,
    }
    return envelope_response(data)


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
