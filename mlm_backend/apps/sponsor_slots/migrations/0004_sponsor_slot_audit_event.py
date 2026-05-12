import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("sponsor_slots", "0003_progressive_unlock"),
    ]

    operations = [
        migrations.CreateModel(
            name="SponsorSlotAuditEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_type", models.CharField(choices=[("ISSUED", "Issued"), ("FLAGGED", "Flagged"), ("AUDIT_CLEARED", "Audit cleared"), ("REDEEMED", "Redeemed"), ("EXPIRED", "Expired"), ("SHARED", "Shared")], max_length=32)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        help_text="Null when the actor is the system.",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="sponsor_slot_audit_actions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "sponsor_slot_code",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="audit_events",
                        to="sponsor_slots.sponsorslotcode",
                    ),
                ),
            ],
            options={
                "db_table": "sponsor_slot_audit_event",
                "ordering": ("-created_at", "-id"),
            },
        ),
    ]
