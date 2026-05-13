from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("admin_panel", "0009_systemconfig_refund_request_sla_hours"),
    ]

    operations = [
        migrations.AddField(
            model_name="systemconfig",
            name="cooling_off_days",
            field=models.PositiveIntegerField(
                default=7,
                help_text="Days recent wallet credits are excluded from withdrawable balance.",
            ),
        ),
    ]
