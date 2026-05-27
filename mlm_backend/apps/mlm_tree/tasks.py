from django.utils import timezone

from config.celery import app

from apps.payments.models import Order

from .placement import try_auto_place_order


@app.task
def auto_place_pending_placements():
    # FAILED orders are retried so the rare child-pays-before-sponsor edge case
    # heals on the next tick after the sponsor is placed (e.g. by their own
    # deadline-driven auto-placement).
    qs = (
        Order.objects.filter(
            placement_status__in=(
                Order.PlacementStatus.PENDING,
                Order.PlacementStatus.FAILED,
            ),
            status=Order.Status.PAID,
            is_retail_purchase=False,
            placement_deadline_at__lte=timezone.now(),
        )
        .select_related("user")
        .order_by("placement_deadline_at", "id")
    )
    for order in qs:
        if hasattr(order.user, "binary_node"):
            continue
        try_auto_place_order(order)
