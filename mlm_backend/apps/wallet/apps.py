from django.apps import AppConfig


class WalletConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.wallet"
    label = "wallet"

    def ready(self):
        # Register the pre_save signal that clears the defer_save sentinel
        # used by apps.wallet.tds_settlement.settle_tds_payable.
        from apps.wallet import tds_settlement  # noqa: F401
