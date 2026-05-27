from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("commissions", "0004_slot_band_held"),
    ]

    operations = [
        migrations.AlterField(
            model_name="commissionledger",
            name="commission_type",
            field=models.CharField(
                max_length=20,
                choices=[
                    ("DIRECT", "Direct"),
                    ("UPLINE_L1", "Upline L1"),
                    ("UPLINE_L2", "Upline L2"),
                    ("UPLINE_L3", "Upline L3"),
                    ("MILESTONE", "Milestone"),
                ],
            ),
        ),
    ]

