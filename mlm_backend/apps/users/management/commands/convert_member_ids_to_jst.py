from __future__ import annotations

from dataclasses import dataclass

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.users.models import User


@dataclass(frozen=True)
class _Change:
    user_pk: int
    old: str
    new: str


def _parse_suffix_digits(member_id: str) -> str | None:
    if not member_id or len(member_id) < 4:
        return None
    suffix = member_id[3:]
    if not suffix.isdigit():
        return None
    return suffix


def _current_max_jst_number() -> int:
    last = (
        User.objects.select_for_update()
        .filter(member_id__startswith="JST")
        .order_by("-member_id")
        .only("member_id")
        .first()
    )
    if not last:
        return 0
    suffix = _parse_suffix_digits(last.member_id)
    if not suffix:
        return 0
    return int(suffix)


class Command(BaseCommand):
    help = "Convert existing users' member_id values to JST-prefixed IDs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show planned changes without writing to DB.",
        )
        parser.add_argument(
            "--only-mlm",
            action="store_true",
            help='Only convert IDs that start with "MLM".',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        dry_run: bool = bool(options["dry_run"])
        only_mlm: bool = bool(options["only_mlm"])

        users = (
            User.objects.select_for_update()
            .all()
            .only("pk", "member_id")
            .order_by("pk")
        )

        next_num = _current_max_jst_number() + 1
        planned: list[_Change] = []

        for u in users:
            old = (u.member_id or "").strip()
            if not old:
                continue
            if old.upper().startswith("JST"):
                continue
            if only_mlm and not old.upper().startswith("MLM"):
                continue

            suffix = _parse_suffix_digits(old)
            candidate = f"JST{suffix}" if suffix else None

            if candidate and not User.objects.exclude(pk=u.pk).filter(member_id=candidate).exists():
                planned.append(_Change(user_pk=u.pk, old=old, new=candidate))
                if not dry_run:
                    User.objects.filter(pk=u.pk).update(member_id=candidate)
                continue

            # Fallback: allocate a fresh unique JST###### for collisions/odd formats.
            while True:
                fresh = f"JST{next_num:06d}"
                next_num += 1
                if not User.objects.filter(member_id=fresh).exists():
                    planned.append(_Change(user_pk=u.pk, old=old, new=fresh))
                    if not dry_run:
                        User.objects.filter(pk=u.pk).update(member_id=fresh)
                    break

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN: no changes were written."))

        self.stdout.write(f"Planned/Applied changes: {len(planned)}")
        for ch in planned[:200]:
            self.stdout.write(f"{ch.user_pk}: {ch.old} -> {ch.new}")
        if len(planned) > 200:
            self.stdout.write(f"... and {len(planned) - 200} more")

