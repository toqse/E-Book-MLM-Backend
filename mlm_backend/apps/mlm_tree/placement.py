"""Placement queue (paid MLM, deferred binary) and completion + commissions."""

from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.admin_panel.utils import get_system_config
from apps.audit.services import write_audit
from apps.commissions.engine import CommissionEngine
from apps.commissions.models import CommissionLedger
from apps.commissions.tasks import process_commission_task
from apps.payments.models import Order
from apps.users.models import User

from .models import BinaryNode
from .services import BinaryTreeService


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
        process_commission_task.delay(order.id)
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
    for o in Order.objects.filter(
        user=buyer,
        status=Order.Status.PAID,
        is_retail_purchase=False,
    ).order_by("id"):
        try:
            process_commission_task.delay(o.id)
        except Exception:
            CommissionEngine.process_order(o)


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
    """Run auto strategy for one PENDING order past deadline. Returns True if placed."""
    cfg = get_system_config()
    buyer = order.user
    if hasattr(buyer, "binary_node"):
        return False
    if order.placement_status != Order.PlacementStatus.PENDING:
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
    # Ensure parent exists in tree.
    if not BinaryNode.objects.filter(user_id=parent_user.pk).exists():
        BinaryTreeService.place_member_auto(parent_user, None, None)
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
