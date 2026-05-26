from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tds", "0001_initial"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="tdsledger",
            name="unique_user_fy",
        ),
        migrations.AddField(
            model_name="tdsledger",
            name="section",
            field=models.CharField(
                choices=[("194H", "194H"), ("194R", "194R")],
                default="194H",
                max_length=10,
            ),
        ),
        migrations.AddConstraint(
            model_name="tdsledger",
            constraint=models.UniqueConstraint(
                fields=("user", "financial_year", "section"),
                name="unique_user_fy_section",
            ),
        ),
    ]
