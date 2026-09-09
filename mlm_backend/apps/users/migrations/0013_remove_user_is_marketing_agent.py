from django.db import migrations, models


def drop_is_marketing_agent_column(apps, schema_editor):
    User = apps.get_model("users", "User")
    table = User._meta.db_table
    connection = schema_editor.connection

    with connection.cursor() as cursor:
        columns = {
            col.name
            for col in connection.introspection.get_table_description(cursor, table)
        }

    if "is_marketing_agent" not in columns:
        return

    field = models.BooleanField(default=False, db_index=True)
    field.set_attributes_from_name("is_marketing_agent")
    field.model = User
    schema_editor.remove_field(User, field)


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0012_user_is_marketing_agent"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(
                    model_name="user",
                    name="is_marketing_agent",
                ),
            ],
            database_operations=[
                migrations.RunPython(
                    drop_is_marketing_agent_column,
                    migrations.RunPython.noop,
                ),
            ],
        ),
    ]
