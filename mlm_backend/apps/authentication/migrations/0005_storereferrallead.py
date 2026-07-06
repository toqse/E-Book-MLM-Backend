from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("authentication", "0004_otprecord_admin_kyc_purpose"),
    ]

    operations = [
        migrations.CreateModel(
            name="StoreReferralLead",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("phone", models.CharField(max_length=20, unique=True)),
                ("referral_code", models.CharField(max_length=32)),
                (
                    "platform",
                    models.CharField(
                        choices=[("ANDROID", "Android"), ("IOS", "iOS")],
                        max_length=16,
                    ),
                ),
                ("expires_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "auth_store_referral_lead",
                "indexes": [models.Index(fields=["expires_at"], name="auth_store__expires_0f0f0f_idx")],
            },
        ),
    ]
