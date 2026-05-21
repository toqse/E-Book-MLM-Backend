from django.db import migrations, models


def backfill_kyc_first_approved_at(apps, schema_editor):
    User = apps.get_model("users", "User")
    for user in User.objects.filter(kyc_status="VERIFIED").iterator():
        if user.kyc_first_approved_at:
            continue
        user.kyc_first_approved_at = user.kyc_reviewed_at or user.updated_at
        user.save(update_fields=["kyc_first_approved_at"])


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0006_user_kyc_invitation_sent_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="kyc_first_approved_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(
            backfill_kyc_first_approved_at,
            migrations.RunPython.noop,
        ),
    ]
