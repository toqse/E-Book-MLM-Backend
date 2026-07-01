from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("authentication", "0003_compliance_and_agreements"),
    ]

    operations = [
        migrations.AlterField(
            model_name="otprecord",
            name="purpose",
            field=models.CharField(
                choices=[
                    ("REGISTER", "Register"),
                    ("LOGIN", "Login"),
                    ("KYC", "KYC"),
                    ("ADMIN_LOGIN", "Admin Login"),
                    ("ADMIN_KYC", "Admin KYC"),
                    ("AGREEMENT", "Agreement"),
                ],
                max_length=20,
            ),
        ),
    ]
