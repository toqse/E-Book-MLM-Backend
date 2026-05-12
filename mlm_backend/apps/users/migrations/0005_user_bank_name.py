# Generated manually for payouts bundle bank_name

from django.db import migrations, models


def backfill_bank_name_from_compliance(apps, schema_editor):
    User = apps.get_model("users", "User")
    MemberComplianceProfile = apps.get_model("agreements", "MemberComplianceProfile")
    for p in MemberComplianceProfile.objects.exclude(bank_name="").iterator():
        name = (p.bank_name or "").strip()
        if not name:
            continue
        u = User.objects.filter(pk=p.user_id).only("bank_name").first()
        if u and not (getattr(u, "bank_name", None) or "").strip():
            User.objects.filter(pk=p.user_id).update(bank_name=name[:255])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0004_aadhaar_len_14"),
        ("agreements", "0001_compliance_and_agreements"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="bank_name",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.RunPython(backfill_bank_name_from_compliance, noop_reverse),
    ]
