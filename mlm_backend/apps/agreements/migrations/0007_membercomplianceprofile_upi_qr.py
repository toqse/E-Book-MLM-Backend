from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("agreements", "0006_unique_pan_aadhaar"),
    ]

    operations = [
        migrations.AddField(
            model_name="membercomplianceprofile",
            name="upi_qr",
            field=models.FileField(
                blank=True,
                null=True,
                upload_to="kyc/upi_qr/%Y/%m/",
            ),
        ),
    ]
