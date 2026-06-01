"""Placement queue (paid MLM, deferred binary) and completion + commissions."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.admin_panel.utils import get_system_config
from apps.audit.services import write_audit
from apps.commissions.engine import CommissionEngine
from apps.commissions.models import CommissionLedger
from apps.commissions.tasks import process_commission_task
from apps.payments.models import Order
from apps.users.models import User
from apps.users.services import company_fallback_sponsor, is_account_capped
from apps.wallet.models import Wallet

from .models import BinaryNode
from .services import BinaryTreeService

logger = logging.getLogger(__name__)


def get_pending_placement_order(user: User) -> Order | None:
    """Paid MLM order row awaiting or recovering from binary placement."""
    return (
        Order.objects.filter(
            user=user,
            status=Order.Status.PAID,
            is_retail_purchase=False,
            placement_status__in=(
                Order.PlacementStatus.PENDING,
                Order.PlacementStatus.FAILED,
            ),
        )
        .order_by("id")
        .first()
    )


def open_placement_queue_if_needed(order: Order, user: User) -> None:
    """After MLM payment: queue first placement or run commissions if already in tree."""
    if order.is_retail_purchase:
        return
    if hasattr(user, "binary_node"):
        order_id = order.id

        def _dispatch() -> None:
            try:
                process_commission_task.delay(order_id)
            except Exception:
                logger.exception("commission_dispatch_failed order_id=%s", order_id)
                try:
                    CommissionEngine.process_order(Order.objects.get(pk=order_id))
                except Exception:
                    logger.exception("commission_inline_fallback_failed order_id=%s", order_id)

        transaction.on_commit(_dispatch)
        return
    if get_pending_placement_order(user):
        return
    cfg = get_system_config()
    order.placement_status = Order.PlacementStatus.PENDING
    order.placement_deadline_at = timezone.now() + timedelta(
        hours=cfg.placement_manual_window_hours
    )
    order.placement_failure_reason = None
    order.save(
        update_fields=[
            "placement_status",
            "placement_deadline_at",
            "placement_failure_reason",
        ]
    )


def finalize_commissions_for_buyer(buyer: User) -> None:
    order_ids = list(
        Order.objects.filter(
            user=buyer,
            status=Order.Status.PAID,
            is_retail_purchase=False,
        )
        .order_by("id")
        .values_list("id", flat=True)
    )

    def _dispatch() -> None:
        for oid in order_ids:
            try:
                process_commission_task.delay(oid)
            except Exception:
                logger.exception("commission_dispatch_failed order_id=%s", oid)
                try:
                    CommissionEngine.process_order(Order.objects.get(pk=oid))
                except Exception:
                    logger.exception("commission_inline_fallback_failed order_id=%s", oid)

    transaction.on_commit(_dispatch)


@transaction.atomic
def complete_placement_for_order(
    order: Order,
    *,
    manual_leg: str | None,
    auto_strategy: str | None,
    final_status: str,
    actor: User | None = None,
    audit_action: str,
) -> None:
    buyer = order.user
    sponsor = buyer.sponsor
    if is_account_capped(sponsor):
        fallback = company_fallback_sponsor()
        if fallback and fallback.id != sponsor.id:
            old_sponsor_id = sponsor.id
            buyer.sponsor = fallback
            buyer.save(update_fields=["sponsor"])
            write_audit(
                "placement.sponsor_reassigned_capped",
                actor=actor,
                target_type="Order",
                target_id=str(order.id),
                payload={"old_sponsor_id": old_sponsor_id, "new_sponsor_id": fallback.id},
            )
            sponsor = fallback
    if manual_leg:
        BinaryTreeService.place_member_manual_leg(buyer, sponsor, manual_leg)
    else:
        BinaryTreeService.place_member_auto(buyer, sponsor, auto_strategy)
    order.placement_status = final_status
    order.placement_resolved_at = timezone.now()
    order.placement_leg_requested = (manual_leg or "").strip().upper() or None
    order.placement_failure_reason = None
    order.save(
        update_fields=[
            "placement_status",
            "placement_resolved_at",
            "placement_leg_requested",
            "placement_failure_reason",
        ]
    )
    finalize_commissions_for_buyer(buyer)
    write_audit(
        audit_action,
        actor=actor,
        target_type="Order",
        target_id=str(order.id),
        payload={
            "buyer_id": buyer.id,
            "member_id": buyer.member_id,
            "placement_status": final_status,
            "leg": manual_leg,
            "strategy": auto_strategy,
        },
    )


def try_auto_place_order(order: Order) -> bool:
    """Run auto strategy for one PENDING/FAILED order past deadline. Returns True if placed."""
    cfg = get_system_config()
    buyer = order.user
    if hasattr(buyer, "binary_node"):
        return False
    if order.placement_status not in (
        Order.PlacementStatus.PENDING,
        Order.PlacementStatus.FAILED,
    ):
        return False
    if order.placement_deadline_at and timezone.now() < order.placement_deadline_at:
        return False
    try:
        complete_placement_for_order(
            order,
            manual_leg=None,
            auto_strategy=cfg.auto_placement_strategy,
            final_status=Order.PlacementStatus.PLACED_AUTO,
            actor=None,
            audit_action="placement.auto",
        )
        return True
    except Exception as exc:  # noqa: BLE001 — log placement failures
        order.placement_status = Order.PlacementStatus.FAILED
        order.placement_failure_reason = str(exc)[:2000]
        order.save(update_fields=["placement_status", "placement_failure_reason"])
        write_audit(
            "placement.auto_failed",
            target_type="Order",
            target_id=str(order.id),
            payload={"error": str(exc)},
        )
        return False


def sponsor_may_manual_place(sponsor: User, buyer: User, order: Order) -> tuple[bool, str]:
    if is_account_capped(sponsor):
        return (
            False,
            "Your account has reached the earning cap. New placements are not permitted.",
        )
    if sponsor.kyc_status != User.KYCStatus.VERIFIED:
        return False, "Complete compliance verification (admin-approved) to access placements."
    try:
        from apps.agreements.models import MemberComplianceProfile

        if not MemberComplianceProfile.objects.filter(user=sponsor).exists():
            return (
                False,
                "Submit compliance details and wait for admin verification to access placements.",
            )
    except Exception:
        return False, "Compliance verification status unavailable; contact support."
    if not hasattr(sponsor, "binary_node"):
        # The sponsor must already be placed in the binary tree before they can host
        # a placement; otherwise we would silently fork the tree into an orphan root.
        return False, (
            "You must be placed in the binary tree by your sponsor "
            "before you can place your referrals."
        )
    if not buyer.sponsor_id:
        return False, "Member has no sponsor"
    if buyer.sponsor_id != sponsor.id:
        return False, "Not your direct referral"
    if order.user_id != buyer.id:
        return False, "Order does not belong to this member"
    if order.placement_status not in (
        Order.PlacementStatus.PENDING,
        Order.PlacementStatus.FAILED,
    ):
        return False, "Placement is not pending on this order"
    if hasattr(buyer, "binary_node"):
        return False, "Member is already placed"
    if order.placement_status == Order.PlacementStatus.PENDING:
        if order.placement_deadline_at and timezone.now() > order.placement_deadline_at:
            return False, "Manual placement window has expired; wait for auto-placement"
    return True, ""


def admin_may_change_placement_in_cooloff(order: Order) -> tuple[bool, str]:
    """
    Admin placement changes are allowed only inside the refund/cool-off window.
    Falls back to paid_at + cfg.refund_window_days when refund_eligible_until is missing.
    """
    if not order or order.status != Order.Status.PAID:
        return False, "Order is not paid"
    cfg = get_system_config()
    now = timezone.now()
    cutoff = order.refund_eligible_until
    if cutoff is None and order.paid_at:
        cutoff = order.paid_at + timedelta(days=cfg.refund_window_days)
    if cutoff is None:
        return False, "Cool-off window unavailable for this order"
    if now > cutoff:
        return False, "Cool-off period ended"
    return True, ""


@transaction.atomic
def admin_place_under_parent(
    *,
    order: Order,
    parent_user: User,
    leg: str,
    actor: User,
    audit_action: str = "placement.admin_place",
) -> None:
    """
    Place (or move) the buyer under a specific parent member on the given leg.
    Leaf-only moves are supported via admin_reverse_placement.
    """
    leg = (leg or "").strip().upper()
    if leg not in (BinaryNode.Position.LEFT, BinaryNode.Position.RIGHT):
        raise ValueError("leg must be LEFT or RIGHT")
    buyer = order.user
    if buyer.id == parent_user.id:
        raise ValueError("Parent cannot be the buyer")
    if not BinaryNode.objects.filter(user_id=parent_user.pk).exists():
        # Refuse to silently create an orphan root for the chosen parent.
        raise ValueError("Selected parent is not placed in the binary tree.")
    parent_node = BinaryNode.objects.select_for_update().get(user_id=parent_user.pk)
    # If buyer already placed, reverse first (leaf-only).
    if hasattr(buyer, "binary_node"):
        admin_reverse_placement(order=order, actor=actor)
        order.refresh_from_db()
    # Place buyer under selected parent leg (spill inside that leg).
    target_parent, target_pos = BinaryTreeService._find_slot_prefer_leg(parent_node, leg)
    BinaryTreeService._attach_under_parent(buyer, target_parent, target_pos)
    order.placement_status = Order.PlacementStatus.PLACED_ADMIN
    order.placement_resolved_at = timezone.now()
    order.placement_leg_requested = leg
    order.placement_failure_reason = None
    order.save(
        update_fields=[
            "placement_status",
            "placement_resolved_at",
            "placement_leg_requested",
            "placement_failure_reason",
        ]
    )
    finalize_commissions_for_buyer(buyer)
    write_audit(
        audit_action,
        actor=actor,
        target_type="Order",
        target_id=str(order.id),
        payload={
            "buyer_id": buyer.id,
            "member_id": buyer.member_id,
            "parent_user_id": parent_user.id,
            "parent_member_id": parent_user.member_id,
            "leg": leg,
        },
    )


@transaction.atomic
def admin_reverse_placement(*, order: Order, actor: User) -> None:
    buyer = order.user
    if not hasattr(buyer, "binary_node"):
        raise ValueError("Member has no binary placement")
    node = buyer.binary_node
    if node.left_child_id or node.right_child_id:
        raise ValueError("Cannot reverse: member has binary downline (leaf-only in v1)")
    for o in Order.objects.filter(
        user=buyer,
        status=Order.Status.PAID,
        is_retail_purchase=False,
    ):
        CommissionEngine.reverse_commissions(o)
    # Detach from parent + maintain cached subtree sizes.
    BinaryTreeService.detach_leaf(node)
    node.delete()
    cfg = get_system_config()
    order.placement_status = Order.PlacementStatus.PENDING
    order.placement_resolved_at = None
    order.placement_leg_requested = None
    order.placement_deadline_at = timezone.now() + timedelta(
        hours=cfg.placement_manual_window_hours
    )
    order.placement_failure_reason = None
    order.save(
        update_fields=[
            "placement_status",
            "placement_resolved_at",
            "placement_leg_requested",
            "placement_deadline_at",
            "placement_failure_reason",
        ]
    )
    write_audit(
        "placement.reverse",
        actor=actor,
        target_type="Order",
        target_id=str(order.id),
        payload={"buyer_id": buyer.id},
    )


def order_has_non_reversed_commissions(order: Order) -> bool:
    return (
        CommissionLedger.objects.filter(order=order)
        .exclude(status=CommissionLedger.Status.REVERSED)
        .exists()
    )


# ---------------------------------------------------------------------------
# Option A: Cascade subtree reverse (separate endpoint; leaf-only reverse above
# is intentionally left untouched).
# ---------------------------------------------------------------------------


@dataclass
class _SubtreeMember:
    node: BinaryNode
    user: User
    order: Order | None
    in_refund_window: bool
    refund_cutoff: object  # datetime | None — kept generic for json serialization
    leaf_first_index: int


@dataclass
class _SubtreePreview:
    root_order: Order
    root_user: User
    members: list[_SubtreeMember]
    affected_orders: list[Order]
    ledger_entries: list[CommissionLedger]
    wallet_snapshots: dict[int, Wallet]  # recipient user_id -> Wallet (or None)
    blocking: list[dict] = field(default_factory=list)

    @property
    def can_reverse(self) -> bool:
        return not self.blocking


def _walk_subtree_nodes(root_node: BinaryNode) -> list[BinaryNode]:
    """
    Return all BinaryNode rows under `root_node` (root included), ordered
    leaves-first so that bottom-up detach is safe (each removal is a leaf).

    Uses post-order DFS over the in-memory `left_child` / `right_child` links
    so we don't need recursive DB queries.
    """
    ordered: list[BinaryNode] = []

    def _post(node: BinaryNode | None) -> None:
        if node is None:
            return
        # Re-fetch fresh references so children/sizes reflect current DB state
        # for the in-transaction caller; the dry-run caller passes the rows it
        # already loaded and is fine with a slight read-vs-write skew (which
        # is exactly why execute re-walks under select_for_update).
        left = (
            BinaryNode.objects.select_related("user").filter(pk=node.left_child_id).first()
            if node.left_child_id
            else None
        )
        right = (
            BinaryNode.objects.select_related("user").filter(pk=node.right_child_id).first()
            if node.right_child_id
            else None
        )
        _post(left)
        _post(right)
        ordered.append(node)

    _post(root_node)
    return ordered


def _refund_cutoff_for_order(order: Order) -> object:
    """Mirror of admin_may_change_placement_in_cooloff's cutoff resolution."""
    cutoff = order.refund_eligible_until
    if cutoff is None and order.paid_at:
        cfg = get_system_config()
        cutoff = order.paid_at + timedelta(days=cfg.refund_window_days)
    return cutoff


def collect_subtree_for_reverse(root_order: Order) -> _SubtreePreview:
    """
    Build a read-only snapshot of the subtree rooted at `root_order.user`.

    For every affected member we also locate their currently-PAID, non-retail
    order (the one that drove the placement & commissions), check its own
    refund window, and load the CommissionLedger entries that would be
    reversed plus the wallet of every recipient.

    Pre-flight: if ANY descendant order is outside its own refund window, the
    operation is refused. The returned preview still lists everything for
    context, but `can_reverse` will be False and `blocking` will list the
    offenders.
    """
    buyer = root_order.user
    if not hasattr(buyer, "binary_node"):
        raise ValueError("Member has no binary placement")

    root_node = buyer.binary_node
    ordered_nodes = _walk_subtree_nodes(root_node)

    now = timezone.now()
    members: list[_SubtreeMember] = []
    affected_orders: list[Order] = []
    blocking: list[dict] = []

    for idx, node in enumerate(ordered_nodes):
        u = node.user
        if node.user_id == buyer.id:
            order_for_member = root_order
        else:
            order_for_member = (
                Order.objects.filter(
                    user_id=u.id,
                    status=Order.Status.PAID,
                    is_retail_purchase=False,
                )
                .order_by("paid_at", "id")
                .first()
            )

        cutoff = (
            _refund_cutoff_for_order(order_for_member) if order_for_member else None
        )
        in_window = bool(cutoff and now <= cutoff)

        members.append(
            _SubtreeMember(
                node=node,
                user=u,
                order=order_for_member,
                in_refund_window=in_window,
                refund_cutoff=cutoff,
                leaf_first_index=idx,
            )
        )
        if order_for_member:
            affected_orders.append(order_for_member)

        if order_for_member is None:
            blocking.append(
                {
                    "member_id": u.member_id,
                    "user_id": u.id,
                    "full_name": u.full_name,
                    "order_id": None,
                    "refund_eligible_until": None,
                    "reason": "no_paid_mlm_order",
                }
            )
        elif not in_window:
            blocking.append(
                {
                    "member_id": u.member_id,
                    "user_id": u.id,
                    "full_name": u.full_name,
                    "order_id": order_for_member.id,
                    "refund_eligible_until": cutoff.isoformat() if cutoff else None,
                    "reason": "outside_refund_window",
                }
            )

    ledger_entries: list[CommissionLedger] = []
    if affected_orders:
        ledger_entries = list(
            CommissionLedger.objects.filter(
                order_id__in=[o.id for o in affected_orders],
                status=CommissionLedger.Status.CREDITED,
            ).select_related("recipient", "source_user", "order")
        )

    recipient_ids = {e.recipient_id for e in ledger_entries}
    wallet_snapshots: dict[int, Wallet] = {}
    for w in Wallet.objects.filter(user_id__in=recipient_ids):
        wallet_snapshots[w.user_id] = w

    return _SubtreePreview(
        root_order=root_order,
        root_user=buyer,
        members=members,
        affected_orders=affected_orders,
        ledger_entries=ledger_entries,
        wallet_snapshots=wallet_snapshots,
        blocking=blocking,
    )


def build_dry_run_payload(preview: _SubtreePreview) -> dict:
    """Format a `_SubtreePreview` into the dry-run JSON response body."""
    root_user = preview.root_user
    root_order = preview.root_order
    root_cutoff = _refund_cutoff_for_order(root_order)

    affected_members_json = []
    placement_resets_json = []
    for m in preview.members:
        node = m.node
        parent_user = node.parent.user if node.parent_id else None
        affected_members_json.append(
            {
                "user_id": m.user.id,
                "member_id": m.user.member_id,
                "full_name": m.user.full_name,
                "level": node.level,
                "leg_position": node.position,
                "parent_member_id": parent_user.member_id if parent_user else None,
                "order_id": m.order.id if m.order else None,
                "order_number": m.order.order_number if m.order else None,
                "refund_eligible_until": (
                    m.refund_cutoff.isoformat() if m.refund_cutoff else None
                ),
                "in_refund_window": m.in_refund_window,
            }
        )
        if m.order:
            placement_resets_json.append(
                {
                    "order_id": m.order.id,
                    "member_id": m.user.member_id,
                    "new_placement_status": Order.PlacementStatus.PENDING,
                    "new_placement_deadline_at": (
                        timezone.now()
                        + timedelta(
                            hours=int(get_system_config().placement_manual_window_hours)
                        )
                    ).isoformat(),
                }
            )

    commission_rows_json = []
    gross_total = Decimal("0")
    net_total = Decimal("0")
    tds_total = Decimal("0")
    debit_by_recipient: dict[int, Decimal] = {}
    tds_unwind_by_recipient: dict[int, Decimal] = {}
    for e in preview.ledger_entries:
        gross_total += e.amount or Decimal("0")
        net_total += e.net_amount or Decimal("0")
        tds_total += e.tds_deducted or Decimal("0")
        if not e.slot_band_held:
            debit_by_recipient[e.recipient_id] = (
                debit_by_recipient.get(e.recipient_id, Decimal("0"))
                + (e.net_amount or Decimal("0"))
            )
        tds_unwind_by_recipient[e.recipient_id] = (
            tds_unwind_by_recipient.get(e.recipient_id, Decimal("0"))
            + (e.tds_deducted or Decimal("0"))
        )
        commission_rows_json.append(
            {
                "ledger_id": e.id,
                "recipient_user_id": e.recipient_id,
                "recipient_member_id": e.recipient.member_id,
                "recipient_full_name": e.recipient.full_name,
                "source_user_id": e.source_user_id,
                "source_member_id": e.source_user.member_id,
                "order_id": e.order_id,
                "order_number": e.order.order_number if e.order_id else None,
                "commission_type": e.commission_type,
                "amount": str(e.amount),
                "tds_deducted": str(e.tds_deducted),
                "net_amount": str(e.net_amount),
                "slot_band_held": bool(e.slot_band_held),
                "status": e.status,
            }
        )

    wallet_impacts_json = []
    wallets_going_negative = 0
    for recipient_id, debit in debit_by_recipient.items():
        w = preview.wallet_snapshots.get(recipient_id)
        if w is None:
            current_balance = Decimal("0")
            current_total_earned = Decimal("0")
        else:
            current_balance = w.cash_balance or Decimal("0")
            current_total_earned = w.total_earned or Decimal("0")
        projected = current_balance - debit
        will_go_negative = projected < Decimal("0")
        if will_go_negative:
            wallets_going_negative += 1
        # Recipient user reference (fall back to wallet relation if present).
        user_obj = None
        for e in preview.ledger_entries:
            if e.recipient_id == recipient_id:
                user_obj = e.recipient
                break
        wallet_impacts_json.append(
            {
                "user_id": recipient_id,
                "member_id": user_obj.member_id if user_obj else None,
                "full_name": user_obj.full_name if user_obj else None,
                "current_cash_balance": str(current_balance),
                "debit_amount": str(debit),
                "projected_cash_balance": str(projected),
                "will_go_negative": will_go_negative,
                "current_total_earned": str(current_total_earned),
                "tds_unwind": str(
                    tds_unwind_by_recipient.get(recipient_id, Decimal("0"))
                ),
            }
        )

    expected_order_ids = sorted([o.id for o in preview.affected_orders])

    return {
        "root": {
            "order_id": root_order.id,
            "order_number": root_order.order_number,
            "member_id": root_user.member_id,
            "full_name": root_user.full_name,
            "refund_eligible_until": (
                root_cutoff.isoformat() if root_cutoff else None
            ),
        },
        "can_reverse": preview.can_reverse,
        "blocking": preview.blocking,
        "summary": {
            "affected_members_count": len(preview.members),
            "affected_orders_count": len(preview.affected_orders),
            "commission_entries_to_reverse": len(preview.ledger_entries),
            "gross_amount_to_reverse": str(gross_total),
            "net_amount_to_reverse": str(net_total),
            "tds_to_unwind": str(tds_total),
            "wallets_going_negative_count": wallets_going_negative,
        },
        "affected_members": affected_members_json,
        "placement_resets": placement_resets_json,
        "commission_reversals": commission_rows_json,
        "wallet_impacts": wallet_impacts_json,
        "expected_root_member_id": root_user.member_id,
        "expected_affected_count": len(preview.members),
        "expected_affected_order_ids": expected_order_ids,
    }


def _reset_order_to_pending(order: Order) -> None:
    cfg = get_system_config()
    order.placement_status = Order.PlacementStatus.PENDING
    order.placement_resolved_at = None
    order.placement_leg_requested = None
    order.placement_deadline_at = timezone.now() + timedelta(
        hours=int(cfg.placement_manual_window_hours)
    )
    order.placement_failure_reason = None
    order.save(
        update_fields=[
            "placement_status",
            "placement_resolved_at",
            "placement_leg_requested",
            "placement_deadline_at",
            "placement_failure_reason",
        ]
    )


@transaction.atomic
def admin_reverse_placement_subtree(
    *,
    root_order: Order,
    actor: User,
    expected_root_member_id: str = "",
    expected_affected_count: int | None = None,
    expected_affected_order_ids: list[int] | None = None,
) -> dict:
    """
    Cascade-reverse the entire binary subtree rooted at `root_order.user`.

    Refuses unless every descendant has its own paid MLM order still inside
    its refund window (mirrors `admin_may_change_placement_in_cooloff`
    semantics per-descendant).

    Re-validates `expected_*` snapshot tokens against the live tree so that a
    UI dry-run cannot be executed if the subtree shifted between preview and
    confirm. Raises ValueError on any mismatch (view layer maps to HTTP 409).
    """
    preview = collect_subtree_for_reverse(root_order)

    if not preview.can_reverse:
        raise ValueError(
            "Cannot reverse subtree: some descendants are outside their refund window "
            "or have no paid MLM order"
        )

    # expected_* checks — protect against TOCTOU between preview and execute.
    if expected_root_member_id and expected_root_member_id != preview.root_user.member_id:
        raise ValueError("expected_root_member_id does not match the order's buyer")
    if (
        expected_affected_count is not None
        and int(expected_affected_count) != len(preview.members)
    ):
        raise ValueError(
            "expected_affected_count does not match current subtree size"
        )
    if expected_affected_order_ids is not None:
        live_ids = sorted([o.id for o in preview.affected_orders])
        if sorted([int(x) for x in expected_affected_order_ids]) != live_ids:
            raise ValueError(
                "expected_affected_order_ids does not match current subtree"
            )

    # Reverse commissions for every affected order (engine is idempotent against
    # already-reversed rows because it only filters CREDITED entries).
    reversed_count = 0
    gross_reversed = Decimal("0")
    net_reversed = Decimal("0")
    for o in preview.affected_orders:
        rows = list(
            CommissionLedger.objects.filter(
                order_id=o.id, status=CommissionLedger.Status.CREDITED
            )
        )
        for r in rows:
            gross_reversed += r.amount or Decimal("0")
            net_reversed += r.net_amount or Decimal("0")
            reversed_count += 1
        CommissionEngine.reverse_commissions(o)

    # Detach + delete binary nodes leaf-first so each delete is a leaf delete.
    for m in preview.members:
        # Re-fetch under lock so cached sizes stay accurate during the loop.
        node = (
            BinaryNode.objects.select_for_update()
            .filter(pk=m.node.pk)
            .first()
        )
        if node is None:
            continue
        if node.left_child_id or node.right_child_id:
            # Defensive: someone re-attached children mid-transaction.
            raise ValueError(
                "Subtree changed during reversal; aborting to keep tree consistent"
            )
        BinaryTreeService.detach_leaf(node)
        node.delete()

    # Reset each affected order to PENDING with a fresh manual window.
    for o in preview.affected_orders:
        _reset_order_to_pending(o)

    affected_order_ids = sorted([o.id for o in preview.affected_orders])
    affected_member_ids = [m.user.member_id for m in preview.members]

    write_audit(
        "placement.reverse_subtree",
        actor=actor,
        target_type="Order",
        target_id=str(root_order.id),
        payload={
            "root_buyer_id": preview.root_user.id,
            "root_member_id": preview.root_user.member_id,
            "affected_order_ids": affected_order_ids,
            "affected_member_ids": affected_member_ids,
            "commission_entries_reversed": reversed_count,
            "gross_reversed": str(gross_reversed),
            "net_reversed": str(net_reversed),
        },
    )

    return {
        "root_order_id": root_order.id,
        "status": "reversed_subtree",
        "affected_order_ids": affected_order_ids,
        "affected_member_ids": affected_member_ids,
        "commission_entries_reversed": reversed_count,
        "gross_reversed": str(gross_reversed),
        "net_reversed": str(net_reversed),
    }
