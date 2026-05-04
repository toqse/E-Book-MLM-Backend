# Generated manually for binary placement queue

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0004_order_ebook"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="placement_deadline_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="order",
            name="placement_failure_reason",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="order",
            name="placement_leg_requested",
            field=models.CharField(blank=True, max_length=10, null=True),
        ),
        migrations.AddField(
            model_name="order",
            name="placement_resolved_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="order",
            name="placement_status",
            field=models.CharField(blank=True, max_length=32, null=True),
        ),
    ]
