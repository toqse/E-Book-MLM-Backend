from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("wallet", "0002_withdrawal_request_lifecycle_fields"),
    ]

    operations = [
        migrations.AlterField(
            model_name="wallettransaction",
            name="tx_type",
            field=models.CharField(
                choices=[
                    ("CREDIT", "Credit"),
                    ("DEBIT", "Debit"),
                    ("ADJUSTMENT", "Adjustment"),
                    ("TDS", "TDS Withheld"),
                ],
                max_length=20,
            ),
        ),
    ]
