from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("admin_panel", "0004_systemconfig_repurchase_commission"),
    ]

    operations = [
        migrations.AddField(
            model_name="systemconfig",
            name="auto_process_milestone_bonuses",
            field=models.BooleanField(
                default=True,
                help_text="If true, milestone bonuses are credited automatically upon achievement; if false, they enter an admin queue for manual processing.",
            ),
        ),
    ]

