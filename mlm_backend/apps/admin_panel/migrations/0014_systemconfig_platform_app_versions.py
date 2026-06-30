from django.db import migrations, models


def copy_legacy_app_version_fields(apps, schema_editor):
    SystemConfig = apps.get_model("admin_panel", "SystemConfig")
    for cfg in SystemConfig.objects.all():
        cfg.ios_latest_app_version = cfg.latest_app_version or ""
        cfg.ios_force_update = bool(cfg.force_update)
        cfg.android_latest_app_version = cfg.latest_app_version or ""
        cfg.android_force_update = bool(cfg.force_update)
        cfg.save(
            update_fields=[
                "ios_latest_app_version",
                "ios_force_update",
                "android_latest_app_version",
                "android_force_update",
            ]
        )


class Migration(migrations.Migration):

    dependencies = [
        ("admin_panel", "0013_systemconfig_msg91_development_mode"),
    ]

    operations = [
        migrations.AddField(
            model_name="systemconfig",
            name="ios_latest_app_version",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="systemconfig",
            name="ios_force_update",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="systemconfig",
            name="android_latest_app_version",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="systemconfig",
            name="android_force_update",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(copy_legacy_app_version_fields, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="systemconfig",
            name="latest_app_version",
        ),
        migrations.RemoveField(
            model_name="systemconfig",
            name="force_update",
        ),
    ]
