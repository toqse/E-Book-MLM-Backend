from collections import deque

from django.db import transaction

from apps.users.models import User

from .models import BinaryNode


class BinaryTreeService:
    @staticmethod
    @transaction.atomic
    def place_member(new_user: User, sponsor_user: User | None) -> BinaryNode:
        if hasattr(new_user, "binary_node"):
            return new_user.binary_node
        if sponsor_user is None:
            node = BinaryNode.objects.create(
                user=new_user,
                parent=None,
                position=None,
                level=1,
                upline_l1=None,
                upline_l2=None,
                upline_l3=None,
            )
            return node

        if sponsor_user.is_member and not hasattr(sponsor_user, "binary_node"):
            BinaryNode.objects.create(
                user=sponsor_user,
                parent=None,
                position=None,
                level=1,
                upline_l1=None,
                upline_l2=None,
                upline_l3=None,
            )

        sponsor_node = BinaryNode.objects.select_for_update().get(user=sponsor_user)

        parent, position = BinaryTreeService._find_slot(sponsor_node)
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
    def _find_slot(sponsor_node: BinaryNode) -> tuple[BinaryNode, str]:
        if sponsor_node.left_child_id is None:
            return sponsor_node, BinaryNode.Position.LEFT
        if sponsor_node.right_child_id is None:
            return sponsor_node, BinaryNode.Position.RIGHT
        q = deque([sponsor_node])
        while q:
            n = q.popleft()
            if n.left_child_id is None:
                return n, BinaryNode.Position.LEFT
            if n.right_child_id is None:
                return n, BinaryNode.Position.RIGHT
            if n.left_child_id:
                q.append(n.left_child)
            if n.right_child_id:
                q.append(n.right_child)
        raise RuntimeError("No free slot in sponsor subtree")
