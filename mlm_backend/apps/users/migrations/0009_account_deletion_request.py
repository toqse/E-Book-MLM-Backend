import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0008_user_aadhaar_unique_pan_aadhaar"),
    ]

    operations = [
        migrations.CreateModel(
            name="AccountDeletionRequest",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("snapshot_member_id", models.CharField(max_length=32)),
                ("snapshot_full_name", models.CharField(max_length=255)),
                (
                    "snapshot_email",
                    models.EmailField(blank=True, max_length=254, null=True),
                ),
                (
                    "snapshot_phone",
                    models.CharField(blank=True, max_length=20, null=True),
                ),
                ("reason", models.TextField()),
                (
                    "status",
                    models.CharField(
                        choices=[("PENDING", "Pending"), ("COMPLETED", "Completed")],
                        default="PENDING",
                        max_length=20,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "completed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="account_deletion_requests_completed",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="account_deletion_requests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "users_account_deletion_request",
            },
        ),
        migrations.AddConstraint(
            model_name="accountdeletionrequest",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "PENDING"), ("user__isnull", False)),
                fields=("user",),
                name="uniq_pending_account_deletion_per_user",
            ),
        ),
    ]
