from config.celery import app

from apps.payments.models import Order, OrderLine
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from apps.users.kyc_eligibility import is_instant_kyc_submission_enabled
from apps.users.kyc_invitation_service import send_kyc_invitation_to_user


def _candidate_user_ids_past_refund() -> list[int]:
    now = timezone.now()
    return list(
        Order.objects.filter(status=Order.Status.PAID, refund_eligible_until__lt=now)
        .filter(
            Q(ebook_id__isnull=False)
            | Exists(OrderLine.objects.filter(order_id=OuterRef("pk")))
        )
        .values_list("user_id", flat=True)
        .distinct()
    )


@app.task
def send_kyc_invitations_after_refund() -> int:
    if is_instant_kyc_submission_enabled():
        return 0
    sent = 0
    for uid in _candidate_user_ids_past_refund():
        if send_kyc_invitation_for_user(uid):
            sent += 1
    return sent


@app.task
def send_kyc_invitation_for_user(user_id: int, *, force: bool = False) -> bool:
    result = send_kyc_invitation_to_user(user_id, force=force)
    return bool(result.get("sent"))
