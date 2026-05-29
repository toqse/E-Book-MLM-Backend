from django.db import migrations, models


def forward_cleanup(apps, schema_editor):
    from apps.agreements.migration_identity_cleanup import run_identity_data_cleanup

    run_identity_data_cleanup(apps)


class Migration(migrations.Migration):

    dependencies = [
        ("agreements", "0005_compliance_required_singleton_constraint"),
        ("users", "0007_user_kyc_first_approved_at"),
    ]

    operations = [
        migrations.RunPython(forward_cleanup, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="membercomplianceprofile",
            constraint=models.UniqueConstraint(
                condition=~models.Q(pan_number=""),
                fields=("pan_number",),
                name="uniq_compliance_profile_pan",
            ),
        ),
        migrations.AddConstraint(
            model_name="membercomplianceprofile",
            constraint=models.UniqueConstraint(
                condition=~models.Q(aadhar_number=""),
                fields=("aadhar_number",),
                name="uniq_compliance_profile_aadhar",
            ),
        ),
    ]
