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


def make_spec(**overrides) -> dict:
    """Spec válida (dict JSON) de SOL-USDC; `overrides` troca chaves do topo.

    Use `StrategySpec.model_validate(make_spec(...))` para o objeto.
    """
    spec = {
        "version": 1,
        "name": "sol-dip",
        "agent_id": "test-agent",
        "rationale": "teste",
        "symbol": "SOL-USDC",
        "timeframe": "1_MINUTE",
        "entry": {
            "mode": "all",
            "conditions": [{"type": "price_below", "value": 100}],
        },
        "exit": {
            "stop": {"type": "stop_loss", "pct": 5},
            "mode": "any",
            "conditions": [{"type": "take_profit", "pct": 10}],
        },
        "sizing": {"type": "fixed_usd", "usd": 20},
        "budget_usd": 50,
        "max_loss_usd": 10,
        "cooldown_minutes": 0,
        "expires_at": "2099-01-01T00:00:00Z",
    }
    spec.update(overrides)
    return spec


def mock_provider(**attrs):
    """`AsyncMock(spec=AsyncJupiterProvider)` com os fatos do local de execução.

    Um mock com spec devolve Mock para propriedades; a conta faz contas com
    `native_fee_reserve`, então ela precisa ser um Decimal de verdade.
    """
    from unittest.mock import AsyncMock

    from trader.providers.jupiter.async_jupiter_svc import AsyncJupiterProvider

    provider = AsyncMock(spec=AsyncJupiterProvider)
    provider.native_fee_reserve = Decimal("0.02")
    provider.balances_track_fills = True
    for name, value in attrs.items():
        setattr(provider, name, value)
    return provider
