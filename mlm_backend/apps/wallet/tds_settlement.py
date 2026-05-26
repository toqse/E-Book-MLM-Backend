"""Settle accumulated Sec 194R TDS payable from wallet cash on cash events."""

from __future__ import annotations

import logging
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models.signals import pre_save
from django.dispatch import receiver

from apps.users.models import User
from apps.wallet.models import Wallet, WalletTransaction

logger = logging.getLogger(__name__)

ZERO = Decimal("0")
_SENTINEL_ATTR = "_tds_settle_needs_save"


def settle_tds_payable(
    *,
    wallet: Wallet,
    recipient: User,
    reference: str | None = None,
    defer_save: bool = False,
) -> Decimal:
    """
    If wallet.tds_payable > 0, debit as much as possible from cash_balance
    and write a TxType.TDS wallet entry tagged section='194R'.

    Mutates `wallet` in-memory. By default this helper also persists the
    wallet itself with explicit `update_fields`. Pass `defer_save=True`
    only when the caller intends to save the wallet in the same
    transaction; the helper installs a runtime guard that raises
    AssertionError in DEBUG/test environments (and logs an error in
    production) if the caller forgets to save.

    Returns the amount settled.
    """
    owed = wallet.tds_payable or ZERO
    cash = wallet.cash_balance or ZERO
    if owed <= ZERO or cash <= ZERO:
        return ZERO

    settle = min(owed, cash)
    wallet.cash_balance = cash - settle
    wallet.tds_payable = owed - settle
    wallet.total_tds_deducted = (wallet.total_tds_deducted or ZERO) + settle

    WalletTransaction.objects.create(
        user=recipient,
        tx_type=WalletTransaction.TxType.TDS,
        amount=settle,
        balance_after=wallet.cash_balance,
        reference=reference or "TDS-194R-SETTLE",
        meta={
            "section": "194R",
            "kind": "tds_payable_settlement",
        },
    )

    if defer_save:
        setattr(wallet, _SENTINEL_ATTR, True)
        wallet_pk = wallet.pk

        def _verify_saved() -> None:
            still_dirty = getattr(wallet, _SENTINEL_ATTR, False)
            if not still_dirty:
                return
            msg = (
                "settle_tds_payable(defer_save=True) was called on "
                f"wallet pk={wallet_pk} but the wallet was never saved. "
                "TDS settlement mutations may have been lost."
            )
            if settings.DEBUG or getattr(settings, "TESTING", False):
                raise AssertionError(msg)
            logger.error(msg)

        transaction.on_commit(_verify_saved)
    else:
        wallet.save(
            update_fields=[
                "cash_balance",
                "tds_payable",
                "total_tds_deducted",
                "updated_at",
            ]
        )

    return settle


@receiver(pre_save, sender=Wallet)
def _clear_tds_settle_sentinel(sender, instance, **kwargs):
    """Mark the in-memory wallet object as saved so the defer_save guard passes."""
    if getattr(instance, _SENTINEL_ATTR, False):
        setattr(instance, _SENTINEL_ATTR, False)
