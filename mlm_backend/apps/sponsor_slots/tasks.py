from django.utils import timezone

from config.celery import app

from .models import SponsorSlotCode


@app.task
def expire_sponsor_slots():
    now = timezone.now()
    qs = SponsorSlotCode.objects.filter(
        status=SponsorSlotCode.Status.ACTIVE, expires_at__lt=now
    )
    qs.update(status=SponsorSlotCode.Status.EXPIRED)
    return qs.count()
