from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("admin_panel", "0011_systemconfig_trigger_instant_kyc_submission"),
    ]

    operations = [
        migrations.AddField(
            model_name="systemconfig",
            name="latest_app_version",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="systemconfig",
            name="force_update",
            field=models.BooleanField(default=False),
        ),
    ]
