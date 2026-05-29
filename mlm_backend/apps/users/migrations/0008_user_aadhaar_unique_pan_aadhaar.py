from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0007_user_kyc_first_approved_at"),
        ("agreements", "0006_unique_pan_aadhaar"),
    ]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="aadhaar_number",
            field=models.CharField(blank=True, max_length=12, null=True),
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.UniqueConstraint(
                condition=models.Q(pan_number__isnull=False)
                & ~models.Q(pan_number=""),
                fields=("pan_number",),
                name="uniq_user_pan_number",
            ),
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.UniqueConstraint(
                condition=models.Q(aadhaar_number__isnull=False)
                & ~models.Q(aadhaar_number=""),
                fields=("aadhaar_number",),
                name="uniq_user_aadhaar_number",
            ),
        ),
    ]
