import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0011_rename_payments_re_status_d55b59_idx_payments_re_status_2707fa_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="refundrequest",
            name="order_line",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="refund_requests",
                to="payments.orderline",
            ),
        ),
    ]
