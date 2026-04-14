from config.celery import app

from apps.commissions.engine import CommissionEngine
from apps.payments.models import Order


@app.task
def process_commission_task(order_id: int):
    order = Order.objects.get(pk=order_id)
    CommissionEngine.process_order(order)
