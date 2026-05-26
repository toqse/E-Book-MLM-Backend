from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("wallet", "0003_wallettransaction_tds_tx_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="wallet",
            name="tds_payable",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
    ]
