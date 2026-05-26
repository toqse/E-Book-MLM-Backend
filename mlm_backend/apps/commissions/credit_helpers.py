"""Shared wallet writes for commission and milestone credits (Model A)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from apps.tds.services import TdsResult
from apps.users.models import User
from apps.wallet.models import Wallet, WalletTransaction

ZERO = Decimal("0")


def write_commission_wallet_entries(
    *,
    wallet: Wallet,
    recipient: User,
    gross: Decimal,
    tds: Decimal,
    ref_credit: str,
    ref_tds: str,
    credit_meta: dict[str, Any],
    tds_meta: dict[str, Any],
) -> None:
    """
    Credit gross to cash, then post a separate TDS withholding row when applicable.
    Mutates wallet.cash_balance in place; caller must wallet.save() afterward.
    """
    gross = gross or ZERO
    tds = tds or ZERO
    wallet.cash_balance = (wallet.cash_balance or ZERO) + gross
    balance_after_credit = wallet.cash_balance
    WalletTransaction.objects.create(
        user=recipient,
        tx_type=WalletTransaction.TxType.CREDIT,
        amount=gross,
        balance_after=balance_after_credit,
        reference=ref_credit,
        meta=credit_meta,
    )
    if tds > ZERO:
        wallet.cash_balance = (wallet.cash_balance or ZERO) - tds
        WalletTransaction.objects.create(
            user=recipient,
            tx_type=WalletTransaction.TxType.TDS,
            amount=tds,
            balance_after=wallet.cash_balance,
            reference=ref_tds,
            meta=tds_meta,
        )


def tds_wallet_meta(tds: TdsResult, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "section": "194H",
        "rate_percent": str(tds.tds_rate_percent),
        "financial_year": tds.financial_year,
        "gross": str(tds.gross_amount),
    }
    if extra:
        meta.update(extra)
    return meta
