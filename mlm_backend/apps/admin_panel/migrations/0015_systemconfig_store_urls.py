from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("admin_panel", "0014_systemconfig_platform_app_versions"),
    ]

    operations = [
        migrations.AddField(
            model_name="systemconfig",
            name="play_store_url",
            field=models.URLField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="systemconfig",
            name="app_store_url",
            field=models.URLField(blank=True, default="", max_length=500),
        ),
    ]
