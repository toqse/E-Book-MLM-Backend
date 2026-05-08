from django.conf import settings
from django.db import models


class BinaryNode(models.Model):
    class Position(models.TextChoices):
        LEFT = "LEFT", "Left"
        RIGHT = "RIGHT", "Right"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="binary_node",
    )
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="children",
    )
    position = models.CharField(
        max_length=10, choices=Position.choices, null=True, blank=True
    )
    level = models.PositiveIntegerField(default=1)
    # Cached subtree sizes (node counts) for fast weak-leg reporting and lists.
    # These counts exclude the node itself: they represent the size of the left/right child subtrees.
    left_subtree_size = models.PositiveIntegerField(default=0, db_index=True)
    right_subtree_size = models.PositiveIntegerField(default=0, db_index=True)
    left_child = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="parent_left",
    )
    right_child = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="parent_right",
    )
    upline_l1 = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="downline_l1",
    )
    upline_l2 = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="downline_l2",
    )
    upline_l3 = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="downline_l3",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "mlm_binary_node"
