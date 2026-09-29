"""Fábricas compartilhadas pelos testes (intenções e ledgers em memória).

`open_ledger()` registra o ledger aberto; a fixture autouse
`close_open_ledgers` (conftest) fecha todos ao fim de cada teste, então
`-W error::ResourceWarning` continua limpo.
"""

from decimal import Decimal

from trader.ledger import Ledger
from trader.models import SOLANA_MINTS
from trader.models.intent import IntentSide, TradeIntent

USDC = SOLANA_MINTS.get_by_symbol("USDC").mint
SOL = SOLANA_MINTS.get_by_symbol("SOL").mint

_OPEN_LEDGERS: list[Ledger] = []


def make_intent(
    side: IntentSide = IntentSide.BUY,
    notional: str | None = "10",
    spend_amount: str = "10",
    key: str | None = None,
    **overrides,
) -> TradeIntent:
    """Compra de 10 USDC -> SOL em `dry:SOL-USDC`; `overrides` troca campos."""
    if key is not None:
        overrides["idempotency_key"] = key
    return TradeIntent(
        source=overrides.pop("source", "test"),
        account=overrides.pop("account", "dry:SOL-USDC"),
        side=side,
        spend_mint=overrides.pop("spend_mint", USDC),
        receive_mint=overrides.pop("receive_mint", SOL),
        spend_amount=Decimal(spend_amount),
        notional_usd=None if notional is None else Decimal(notional),
        **overrides,
    )


def open_ledger() -> Ledger:
    """Ledger em memória, fechado automaticamente ao fim do teste."""
    ledger = Ledger()
    _OPEN_LEDGERS.append(ledger)
    return ledger


def close_open_ledgers() -> None:
    while _OPEN_LEDGERS:
        _OPEN_LEDGERS.pop().close()
