import secrets
from decimal import Decimal
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.admin_panel.models import SystemConfig
from apps.payments.models import Order
from apps.wallet.bands import BAND_EDGES

from .audit_log import log_sponsor_audit
from .models import SponsorSlotAuditEvent, SponsorSlotBatch, SponsorSlotCode


class SponsorSlotService:
    @staticmethod
    def _band_range_for_number(band_number: int) -> tuple[Decimal, Decimal | None]:
        if band_number <= 0 or band_number > len(BAND_EDGES):
            raise ValueError("Invalid band number")
        low = Decimal(BAND_EDGES[band_number - 1])
        high = Decimal(BAND_EDGES[band_number]) if band_number < len(BAND_EDGES) else None
        return low, high

    @staticmethod
    @transaction.atomic
    def unlock_due_codes(*, user, total_earned: Decimal) -> int:
        """
        Unlock codes whose unlock threshold is reached.
        Returns number of codes unlocked in this call.
        """
        now = timezone.now()
        qs = SponsorSlotCode.objects.select_for_update().filter(
            issued_to=user,
            status=SponsorSlotCode.Status.LOCKED,
            expires_at__gt=now,
            unlock_at_total_earned__isnull=False,
            unlock_at_total_earned__lte=total_earned,
        )
        n = qs.count()
        if n:
            qs.update(status=SponsorSlotCode.Status.ACTIVE, unlocked_at=now)
        return n

    @staticmethod
    @transaction.atomic
    def issue_batch(
        user,
        band_number: int,
        cfg: SystemConfig | None = None,
        *,
        current_total_earned: Decimal | None = None,
    ) -> SponsorSlotBatch:
        if cfg is None:
            from apps.admin_panel.utils import get_system_config

            cfg = get_system_config()
        expires_at = timezone.now() + timedelta(days=cfg.sponsor_slot_expiry_days)
        low, high = SponsorSlotService._band_range_for_number(band_number)
        total_codes = 5
        # Progressive unlock inside the band:
        # For example Band 2 (₹4000-₹5000) with 5 codes => unlock at ₹4200, ₹4400, ₹4600, ₹4800, ₹5000.
        unlock_thresholds: list[Decimal | None] = []
        if high is not None:
            span = (high - low)
            step = (span / Decimal(total_codes)) if span > 0 else Decimal("0")
            for i in range(total_codes):
                unlock_thresholds.append((low + (step * Decimal(i + 1))).quantize(Decimal("0.01")))
        else:
            unlock_thresholds = [None for _ in range(total_codes)]
        batch = SponsorSlotBatch.objects.create(
            issued_to=user,
            band_number=band_number,
            total_codes=total_codes,
            expires_at=expires_at,
        )
        for i in range(total_codes):
            code_str = "SP-" + secrets.token_hex(3).upper()
            while SponsorSlotCode.objects.filter(code__iexact=code_str).exists():
                code_str = "SP-" + secrets.token_hex(3).upper()
            c = SponsorSlotCode.objects.create(
                batch=batch,
                issued_to=user,
                code=code_str,
                status=SponsorSlotCode.Status.LOCKED if unlock_thresholds[i] is not None else SponsorSlotCode.Status.ACTIVE,
                unlock_at_total_earned=unlock_thresholds[i],
                expires_at=expires_at,
            )
            log_sponsor_audit(
                c,
                SponsorSlotAuditEvent.EventType.ISSUED,
                actor=None,
                metadata={"batch_id": batch.id, "band_number": band_number},
            )
        if current_total_earned is not None:
            SponsorSlotService.unlock_due_codes(user=user, total_earned=Decimal(current_total_earned))
        return batch

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
        log_sponsor_audit(
            code,
            SponsorSlotAuditEvent.EventType.REDEEMED,
            actor=redeemer,
            metadata={"order_id": order.id, "order_number": order.order_number},
        )
        batch = code.batch
        batch.codes_redeemed += 1
        batch.save(update_fields=["codes_redeemed"])
