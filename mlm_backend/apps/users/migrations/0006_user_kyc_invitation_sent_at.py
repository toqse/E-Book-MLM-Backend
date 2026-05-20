from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0005_user_bank_name"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="kyc_invitation_sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
