from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("admin_panel", "0003_systemconfig_placement"),
    ]

    operations = [
        migrations.AddField(
            model_name="systemconfig",
            name="is_repurchase_commission_allowed",
            field=models.BooleanField(
                default=False,
                help_text="If false, no MLM commissions on a buyer's 2nd+ paid non-retail orders.",
            ),
        ),
    ]
