from django.db import migrations, models


def ensure_is_marketing_agent_column(apps, schema_editor):
    """Align DB with model: add column or set DEFAULT on orphaned MySQL columns."""
    User = apps.get_model("users", "User")
    table = User._meta.db_table
    connection = schema_editor.connection

    with connection.cursor() as cursor:
        columns = {
            col.name
            for col in connection.introspection.get_table_description(cursor, table)
        }

    if "is_marketing_agent" in columns:
        if connection.vendor == "mysql":
            with connection.cursor() as cursor:
                cursor.execute(
                    f"ALTER TABLE `{table}` MODIFY COLUMN `is_marketing_agent` "
                    "tinyint(1) NOT NULL DEFAULT 0"
                )
        return

    field = models.BooleanField(default=False, db_index=True)
    field.set_attributes_from_name("is_marketing_agent")
    field.model = User
    schema_editor.add_field(User, field)


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0011_user_signup_referral_fields"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="user",
                    name="is_marketing_agent",
                    field=models.BooleanField(db_index=True, default=False),
                ),
            ],
            database_operations=[
                migrations.RunPython(
                    ensure_is_marketing_agent_column,
                    migrations.RunPython.noop,
                ),
            ],
        ),
    ]
