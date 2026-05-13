import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("payments", "0008_cart_and_orderline"),
    ]

    operations = [
        migrations.CreateModel(
            name="RefundRequest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("reference", models.CharField(max_length=48, unique=True)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                (
                    "payment_method",
                    models.CharField(
                        choices=[("GATEWAY", "Gateway"), ("DIRECT", "Direct")],
                        default="GATEWAY",
                        max_length=16,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("PROCESSING", "Processing"),
                            ("APPROVED", "Approved"),
                            ("REJECTED", "Rejected"),
                        ],
                        default="PENDING",
                        max_length=20,
                    ),
                ),
                ("member_note", models.TextField(blank=True, default="")),
                ("reject_reason", models.TextField(blank=True, default="")),
                ("processing_at", models.DateTimeField(blank=True, null=True)),
                ("approved_at", models.DateTimeField(blank=True, null=True)),
                ("rejected_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "approved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="approved_refund_requests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="refund_requests",
                        to="payments.order",
                    ),
                ),
                (
                    "processing_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="refund_requests_marked_processing",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "rejected_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="rejected_refund_requests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="refund_requests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "payments_refundrequest",
            },
        ),
        migrations.AddIndex(
            model_name="refundrequest",
            index=models.Index(fields=["status", "-created_at"], name="payments_re_status_d55b59_idx"),
        ),
        migrations.AddIndex(
            model_name="refundrequest",
            index=models.Index(fields=["user", "-created_at"], name="payments_re_user_id_82e6ad_idx"),
        ),
    ]
