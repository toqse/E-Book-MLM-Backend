from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0009_refundrequest"),
    ]

    operations = [
        migrations.AddField(
            model_name="refundrequest",
            name="razorpay_refund_id",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
    ]
