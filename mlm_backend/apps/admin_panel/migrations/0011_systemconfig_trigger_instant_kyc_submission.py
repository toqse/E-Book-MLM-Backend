from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("admin_panel", "0010_systemconfig_cooling_off_days"),
    ]

    operations = [
        migrations.AddField(
            model_name="systemconfig",
            name="trigger_instant_kyc_submission",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "When true, members may submit KYC/compliance immediately after a PAID ebook "
                    "purchase (no refund-window wait, no invitation email/SMS). When false, "
                    "submission opens only after any purchase refund window closes and an "
                    "invitation is sent."
                ),
            ),
        ),
    ]
