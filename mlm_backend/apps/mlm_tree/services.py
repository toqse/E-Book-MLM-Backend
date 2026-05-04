from collections import deque

from django.db import transaction

from apps.admin_panel.models import SystemConfig
from apps.users.models import User

from .models import BinaryNode


class BinaryTreeService:
    """Binary tree placement: spillover, manual leg preference, and auto strategies."""

    @staticmethod
    def subtree_node_count(node: BinaryNode | None) -> int:
        if node is None:
            return 0
        count = 0
        stack = [node]
        while stack:
            n = stack.pop()
            count += 1
            if n.left_child_id:
                stack.append(n.left_child)
            if n.right_child_id:
                stack.append(n.right_child)
        return count

    @staticmethod
    def _find_slot_left_first(sponsor_node: BinaryNode) -> tuple[BinaryNode, str]:
        pos = BinaryNode.Position
        if sponsor_node.left_child_id is None:
            return sponsor_node, pos.LEFT
        if sponsor_node.right_child_id is None:
            return sponsor_node, pos.RIGHT
        q = deque([sponsor_node])
        while q:
            n = q.popleft()
            if n.left_child_id is None:
                return n, pos.LEFT
            if n.right_child_id is None:
                return n, pos.RIGHT
            if n.left_child_id:
                q.append(n.left_child)
            if n.right_child_id:
                q.append(n.right_child)
        raise RuntimeError("No free slot in sponsor subtree")

    @staticmethod
    def _find_slot_right_first(sponsor_node: BinaryNode) -> tuple[BinaryNode, str]:
        pos = BinaryNode.Position
        if sponsor_node.right_child_id is None:
            return sponsor_node, pos.RIGHT
        if sponsor_node.left_child_id is None:
            return sponsor_node, pos.LEFT
        q = deque([sponsor_node.right_child, sponsor_node.left_child])
        while q:
            n = q.popleft()
            if n.right_child_id is None:
                return n, pos.RIGHT
            if n.left_child_id is None:
                return n, pos.LEFT
            if n.right_child_id:
                q.append(n.right_child)
            if n.left_child_id:
                q.append(n.left_child)
        raise RuntimeError("No free slot in sponsor subtree")

    @staticmethod
    def _find_slot_prefer_leg(sponsor_node: BinaryNode, leg: str) -> tuple[BinaryNode, str]:
        """Only under the given direct leg of sponsor (spill inside that subtree)."""
        pos = BinaryNode.Position
        if leg == pos.LEFT:
            if sponsor_node.left_child_id is None:
                return sponsor_node, pos.LEFT
            q = deque([sponsor_node.left_child])
            while q:
                n = q.popleft()
                if n.left_child_id is None:
                    return n, pos.LEFT
                if n.right_child_id is None:
                    return n, pos.RIGHT
                q.append(n.left_child)
                q.append(n.right_child)
            raise RuntimeError("No free slot in sponsor left leg")
        if leg == pos.RIGHT:
            if sponsor_node.right_child_id is None:
                return sponsor_node, pos.RIGHT
            q = deque([sponsor_node.right_child])
            while q:
                n = q.popleft()
                if n.right_child_id is None:
                    return n, pos.RIGHT
                if n.left_child_id is None:
                    return n, pos.LEFT
                q.append(n.right_child)
                q.append(n.left_child)
            raise RuntimeError("No free slot in sponsor right leg")
        raise ValueError("leg must be LEFT or RIGHT")

    @staticmethod
    def find_slot_for_auto_strategy(sponsor_node: BinaryNode, strategy: str) -> tuple[BinaryNode, str]:
        pos = BinaryNode.Position
        if strategy == SystemConfig.AutoPlacementStrategy.RIGHT_FIRST:
            return BinaryTreeService._find_slot_right_first(sponsor_node)
        if strategy == SystemConfig.AutoPlacementStrategy.LONG_LEG:
            sl = BinaryTreeService.subtree_node_count(sponsor_node.left_child)
            sr = BinaryTreeService.subtree_node_count(sponsor_node.right_child)
            primary = pos.LEFT if sl >= sr else pos.RIGHT
            secondary = pos.RIGHT if primary == pos.LEFT else pos.LEFT
            for leg in (primary, secondary):
                try:
                    return BinaryTreeService._find_slot_prefer_leg(sponsor_node, leg)
                except RuntimeError:
                    continue
            return BinaryTreeService._find_slot_left_first(sponsor_node)
        if strategy == SystemConfig.AutoPlacementStrategy.WEAK_LEG:
            sl = BinaryTreeService.subtree_node_count(sponsor_node.left_child)
            sr = BinaryTreeService.subtree_node_count(sponsor_node.right_child)
            primary = pos.LEFT if sl <= sr else pos.RIGHT
            secondary = pos.RIGHT if primary == pos.LEFT else pos.LEFT
            for leg in (primary, secondary):
                try:
                    return BinaryTreeService._find_slot_prefer_leg(sponsor_node, leg)
                except RuntimeError:
                    continue
            return BinaryTreeService._find_slot_left_first(sponsor_node)
        return BinaryTreeService._find_slot_left_first(sponsor_node)

    @staticmethod
    def _find_slot(sponsor_node: BinaryNode) -> tuple[BinaryNode, str]:
        return BinaryTreeService._find_slot_left_first(sponsor_node)

    @staticmethod
    def _attach_under_parent(
        new_user: User, parent: BinaryNode, position: str
    ) -> BinaryNode:
        level = parent.level + 1
        upline_l1 = parent.user
        upline_l2 = parent.parent.user if parent.parent_id else None
        upline_l3 = (
            parent.parent.parent.user
            if parent.parent_id and parent.parent.parent_id
            else None
        )
        node = BinaryNode.objects.create(
            user=new_user,
            parent=parent,
            position=position,
            level=level,
            upline_l1=upline_l1,
            upline_l2=upline_l2,
            upline_l3=upline_l3,
        )
        if position == BinaryNode.Position.LEFT:
            parent.left_child = node
            parent.save(update_fields=["left_child"])
        else:
            parent.right_child = node
            parent.save(update_fields=["right_child"])
        return node

    @staticmethod
    @transaction.atomic
    def place_member(new_user: User, sponsor_user: User | None) -> BinaryNode:
        """Legacy: left-first spillover (same as auto LEFT_FIRST)."""
        return BinaryTreeService.place_member_auto(new_user, sponsor_user, None)

    @staticmethod
    @transaction.atomic
    def place_member_auto(
        new_user: User, sponsor_user: User | None, strategy: str | None
    ) -> BinaryNode:
        if hasattr(new_user, "binary_node"):
            return new_user.binary_node
        if sponsor_user is None:
            return BinaryTreeService._create_root_node(new_user)
        if not BinaryNode.objects.filter(user_id=sponsor_user.pk).exists():
            BinaryTreeService._create_root_node(sponsor_user)
        sponsor_node = BinaryNode.objects.select_for_update().get(user=sponsor_user)
        strat = strategy or SystemConfig.AutoPlacementStrategy.LEFT_FIRST
        parent, position = BinaryTreeService.find_slot_for_auto_strategy(sponsor_node, strat)
        return BinaryTreeService._attach_under_parent(new_user, parent, position)

    @staticmethod
    @transaction.atomic
    def place_member_manual_leg(new_user: User, sponsor_user: User, leg: str) -> BinaryNode:
        if hasattr(new_user, "binary_node"):
            return new_user.binary_node
        if sponsor_user is None:
            raise ValueError("Manual leg placement requires a sponsor")
        leg = leg.strip().upper()
        pos = BinaryNode.Position
        if leg not in (pos.LEFT, pos.RIGHT):
            raise ValueError("leg must be LEFT or RIGHT")
        if not BinaryNode.objects.filter(user_id=sponsor_user.pk).exists():
            BinaryTreeService._create_root_node(sponsor_user)
        sponsor_node = BinaryNode.objects.select_for_update().get(user=sponsor_user)
        parent, position = BinaryTreeService._find_slot_prefer_leg(sponsor_node, leg)
        return BinaryTreeService._attach_under_parent(new_user, parent, position)

    @staticmethod
    def _create_root_node(user: User) -> BinaryNode:
        return BinaryNode.objects.create(
            user=user,
            parent=None,
            position=None,
            level=1,
            upline_l1=None,
            upline_l2=None,
            upline_l3=None,
        )
