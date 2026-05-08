from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.sponsor_slots.models import SponsorSlotBatch, SponsorSlotCode


class Command(BaseCommand):
    help = "Create (or ensure) a single fixed sponsor slot code for testing."

    def add_arguments(self, parser):
        parser.add_argument(
            "--code",
            type=str,
            default="SPONSOR-TEST",
            help="Sponsor slot code to create (default: SPONSOR-TEST).",
        )
        parser.add_argument(
            "--issuer-user-id",
            type=int,
            default=None,
            help="User id who will own/issue the code (recommended). If omitted, uses the first staff user.",
        )
        parser.add_argument(
            "--expires-in-days",
            type=int,
            default=3650,
            help="Expiry window in days (default: 3650 ~ 10 years).",
        )
        parser.add_argument(
            "--band-number",
            type=int,
            default=2,
            help="Band number to store on SponsorSlotBatch (default: 2).",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        code = (options.get("code") or "").strip()
        if not code:
            raise SystemExit("--code is required")
        if len(code) > 32:
            raise SystemExit("--code must be <= 32 chars")

        issuer_user_id = options.get("issuer_user_id")
        expires_in_days = int(options.get("expires_in_days") or 0)
        band_number = int(options.get("band_number") or 0)
        if expires_in_days <= 0:
            raise SystemExit("--expires-in-days must be > 0")
        if band_number <= 0:
            raise SystemExit("--band-number must be > 0")

        # Resolve issuer without creating users implicitly (safer).
        from django.contrib.auth import get_user_model

        User = get_user_model()
        if issuer_user_id:
            issuer = User.objects.filter(pk=issuer_user_id).first()
        else:
            issuer = User.objects.filter(is_staff=True).order_by("id").first()
        if not issuer:
            raise SystemExit(
                "No issuer user found. Create an admin/staff user and pass --issuer-user-id=<id>."
            )

        expires_at = timezone.now() + timedelta(days=expires_in_days)

        existing = SponsorSlotCode.objects.filter(code__iexact=code).first()
        if existing:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Code already exists: code={existing.code} status={existing.status} "
                    f"issued_to={existing.issued_to_id} expires_at={existing.expires_at.isoformat()}"
                )
            )
            return

        batch = SponsorSlotBatch.objects.create(
            issued_to=issuer,
            band_number=band_number,
            total_codes=1,
            expires_at=expires_at,
        )
        SponsorSlotCode.objects.create(
            batch=batch,
            issued_to=issuer,
            code=code,
            status=SponsorSlotCode.Status.ACTIVE,
            unlock_at_total_earned=Decimal("0.00"),
            unlocked_at=timezone.now(),
            expires_at=expires_at,
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Created sponsor slot code: {code} | issuer_user_id={issuer.id} | expires_at={expires_at.isoformat()}"
            )
        )

