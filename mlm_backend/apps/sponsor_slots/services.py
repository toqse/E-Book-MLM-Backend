import secrets
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.admin_panel.models import SystemConfig
from apps.payments.models import Order

from .models import SponsorSlotBatch, SponsorSlotCode


class SponsorSlotService:
    @staticmethod
    @transaction.atomic
    def issue_batch(user, band_number: int, cfg: SystemConfig | None = None):
        if cfg is None:
            from apps.admin_panel.utils import get_system_config

            cfg = get_system_config()
        expires_at = timezone.now() + timedelta(days=cfg.sponsor_slot_expiry_days)
        batch = SponsorSlotBatch.objects.create(
            issued_to=user,
            band_number=band_number,
            total_codes=5,
            expires_at=expires_at,
        )
        for _ in range(5):
            code_str = "SPONSOR-" + secrets.token_hex(3).upper()
            while SponsorSlotCode.objects.filter(code=code_str).exists():
                code_str = "SPONSOR-" + secrets.token_hex(3).upper()
            SponsorSlotCode.objects.create(
                batch=batch,
                issued_to=user,
                code=code_str,
                expires_at=expires_at,
            )

    @staticmethod
    def validate_code(code: str, redeemer=None) -> SponsorSlotCode | None:
        if not code:
            return None
        c = (
            SponsorSlotCode.objects.select_related("issued_to", "batch")
            .filter(code__iexact=code.strip())
            .first()
        )
        if not c or c.status != SponsorSlotCode.Status.ACTIVE:
            return None
        if timezone.now() > c.expires_at:
            c.status = SponsorSlotCode.Status.EXPIRED
            c.save(update_fields=["status"])
            return None
        if redeemer and c.issued_to_id == redeemer.id:
            return None
        return c

    @staticmethod
    @transaction.atomic
    def redeem_on_order(code: SponsorSlotCode, order: Order, redeemer):
        code.status = SponsorSlotCode.Status.REDEEMED
        code.redeemed_by = redeemer
        code.redeemed_order = order
        code.save()
        batch = code.batch
        batch.codes_redeemed += 1
        batch.save(update_fields=["codes_redeemed"])
