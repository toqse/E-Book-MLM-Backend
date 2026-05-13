from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("admin_panel", "0006_systemconfig_admin_ui_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="systemconfig",
            name="default_company_referral_code",
            field=models.CharField(
                blank=True,
                default="",
                max_length=64,
                help_text=(
                    "When non-empty, overrides DEFAULT_COMPANY_REFERRAL_CODE from the environment "
                    "for sponsor resolution and reserved-code checks. Empty uses the env value."
                ),
            ),
        ),
    ]
