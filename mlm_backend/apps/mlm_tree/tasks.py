from django.utils import timezone

from config.celery import app

from apps.payments.models import Order

from .placement import try_auto_place_order


@app.task
def auto_place_pending_placements():
    qs = (
        Order.objects.filter(
            placement_status=Order.PlacementStatus.PENDING,
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
