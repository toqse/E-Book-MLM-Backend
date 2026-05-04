# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("admin_panel", "0002_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="systemconfig",
            name="auto_placement_strategy",
            field=models.CharField(
                choices=[
                    ("LEFT_FIRST", "Left first (default spillover)"),
                    ("RIGHT_FIRST", "Right first"),
                    ("LONG_LEG", "Long leg (larger subtree first)"),
                    ("WEAK_LEG", "Weak leg (smaller subtree first)"),
                ],
                default="LEFT_FIRST",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="systemconfig",
            name="placement_manual_window_hours",
            field=models.PositiveIntegerField(default=24),
        ),
    ]
