from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("admin_panel", "0008_alter_systemconfig_default_company_referral_code"),
    ]

    operations = [
        migrations.AddField(
            model_name="systemconfig",
            name="refund_request_sla_hours",
            field=models.PositiveIntegerField(
                default=48,
                help_text="Hours from member request submission for open refund SLA (dashboard metrics).",
            ),
        ),
    ]
