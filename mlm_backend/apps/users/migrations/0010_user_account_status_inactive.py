from django.db import migrations, models
from django.db.models import Exists, OuterRef, Q


PROTECTED_STATUSES = ("SUSPENDED", "CAPPED", "DEACTIVATED")


def _paid_ebook_orders_exist(apps):
    Order = apps.get_model("payments", "Order")
    OrderLine = apps.get_model("payments", "OrderLine")

    return Order.objects.filter(user_id=OuterRef("pk"), status="PAID").filter(
        Q(ebook_id__isnull=False)
        | Exists(OrderLine.objects.filter(order_id=OuterRef("pk")))
    )


def backfill_account_status(apps, schema_editor):
    User = apps.get_model("users", "User")
    paid_exists = _paid_ebook_orders_exist(apps)

    members = User.objects.filter(is_staff=False, is_superuser=False)

    members.filter(Exists(paid_exists)).exclude(
        account_status__in=PROTECTED_STATUSES
    ).update(account_status="ACTIVE")

    members.exclude(Exists(paid_exists)).exclude(
        account_status__in=PROTECTED_STATUSES
    ).update(account_status="INACTIVE")


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0008_cart_and_orderline"),
        ("users", "0009_account_deletion_request"),
    ]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="account_status",
            field=models.CharField(
                choices=[
                    ("INACTIVE", "Inactive"),
                    ("ACTIVE", "Active"),
                    ("CAPPED", "Capped"),
                    ("SUSPENDED", "Suspended"),
                    ("DEACTIVATED", "Deactivated"),
                ],
                default="INACTIVE",
                max_length=20,
            ),
        ),
        migrations.RunPython(backfill_account_status, noop_reverse),
    ]
