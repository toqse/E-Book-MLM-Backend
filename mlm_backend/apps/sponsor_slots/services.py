import secrets
from dataclasses import dataclass
from decimal import Decimal
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.admin_panel.models import SystemConfig
from apps.payments.models import Order
from apps.users.services import is_account_capped
from apps.wallet.bands import BAND_EDGES

from .audit_log import log_sponsor_audit
from .models import SponsorSlotAuditEvent, SponsorSlotBatch, SponsorSlotCode


@dataclass(frozen=True)
class SponsorCodeValidation:
    """Outcome of a sponsor-slot code validation.

    `reason` is one of:
      - None        : code is valid
      - "invalid"   : code unknown, not active (locked/redeemed/shared), or self-redemption
      - "expired"   : code's expires_at is in the past (or status was already EXPIRED)
      - "sponsor_inactive" : issuer has reached the earning cap (CAPPED)
    """

    code: SponsorSlotCode | None
    reason: str | None

    @property
    def valid(self) -> bool:
        return self.code is not None and self.reason is None

    @property
    def is_expired(self) -> bool:
        return self.reason == "expired"

    @property
    def is_invalid(self) -> bool:
        return self.reason == "invalid"


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
    def validate_code_detailed(code: str, redeemer=None) -> SponsorCodeValidation:
        """Validate a sponsor-slot code and return the outcome with a reason.

        Distinguishes between:
          - invalid : unknown code, non-active status (locked/redeemed/shared), or self-redemption
          - expired : code's expires_at has passed (status is/becomes EXPIRED)
        """
        if not code:
            return SponsorCodeValidation(code=None, reason="invalid")
        c = (
            SponsorSlotCode.objects.select_related("issued_to", "batch")
            .filter(code__iexact=code.strip())
            .first()
        )
        if not c:
            return SponsorCodeValidation(code=None, reason="invalid")
        if c.status == SponsorSlotCode.Status.EXPIRED:
            return SponsorCodeValidation(code=None, reason="expired")
        if timezone.now() > c.expires_at:
            c.status = SponsorSlotCode.Status.EXPIRED
            c.save(update_fields=["status"])
            return SponsorCodeValidation(code=None, reason="expired")
        if c.status != SponsorSlotCode.Status.ACTIVE:
            return SponsorCodeValidation(code=None, reason="invalid")
        if redeemer and c.issued_to_id == redeemer.id:
            return SponsorCodeValidation(code=None, reason="invalid")
        if is_account_capped(c.issued_to):
            return SponsorCodeValidation(code=None, reason="sponsor_inactive")
        return SponsorCodeValidation(code=c, reason=None)

    @staticmethod
    def validate_code(code: str, redeemer=None) -> SponsorSlotCode | None:
        """Backwards-compatible wrapper that returns the code if valid, else None.

        Callers needing to distinguish between invalid and expired should use
        `validate_code_detailed` instead.
        """
        return SponsorSlotService.validate_code_detailed(code, redeemer=redeemer).code

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
