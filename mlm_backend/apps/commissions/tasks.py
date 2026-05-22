import logging

from config.celery import app

from apps.commissions.engine import CommissionEngine
from apps.payments.models import Order

logger = logging.getLogger(__name__)


@app.task(bind=True, autoretry_for=(Order.DoesNotExist,), retry_backoff=2, max_retries=3)
def process_commission_task(self, order_id: int):
    logger.info(
        "commission_task_start order_id=%s task_id=%s",
        order_id,
        self.request.id,
    )
    try:
        order = Order.objects.select_related("user", "user__sponsor").get(pk=order_id)
    except Order.DoesNotExist:
        logger.warning(
            "commission_task_order_missing order_id=%s (will retry)",
            order_id,
        )
        raise
    logger.info(
        "commission_task_order_loaded order_id=%s status=%s is_retail=%s sponsor_id=%s",
        order.id,
        order.status,
        order.is_retail_purchase,
        getattr(order.user, "sponsor_id", None),
    )
    try:
        CommissionEngine.process_order(order)
    except Exception:
        logger.exception("commission_task_failed order_id=%s", order_id)
        raise
    logger.info("commission_task_done order_id=%s", order_id)
