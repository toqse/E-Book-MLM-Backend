from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("admin_panel", "0005_systemconfig_auto_process_milestone_bonuses"),
    ]

    operations = [
        migrations.AddField(
            model_name="systemconfig",
            name="milestone_bonus_overrides",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Optional mapping of milestone referral threshold -> bonus gross amount. Keys may be int or stringified int.",
            ),
        ),
        migrations.AddField(
            model_name="systemconfig",
            name="razorpay_key_id",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="systemconfig",
            name="razorpay_key_secret",
            field=models.CharField(blank=True, default="", max_length=256),
        ),
        migrations.AddField(
            model_name="systemconfig",
            name="nodal_officer_name",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="systemconfig",
            name="nodal_officer_email",
            field=models.EmailField(blank=True, default="", max_length=254),
        ),
        migrations.AddField(
            model_name="systemconfig",
            name="nodal_officer_phone",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="systemconfig",
            name="grievance_sla_hours",
            field=models.PositiveIntegerField(default=48),
        ),
    ]

