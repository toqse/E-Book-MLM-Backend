from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("admin_panel", "0012_systemconfig_app_version_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="systemconfig",
            name="development_mode",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "When true, skip MSG91 calls and return OTP in API responses (dev/testing). "
                    "When false, send real OTPs, invoices, and invitations via MSG91."
                ),
            ),
        ),
        migrations.AddField(
            model_name="systemconfig",
            name="msg91_authkey",
            field=models.CharField(
                blank=True,
                default="",
                help_text="MSG91 authkey for campaign API; stored write-only in admin config.",
                max_length=128,
            ),
        ),
    ]
