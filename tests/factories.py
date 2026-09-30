"""Fábricas compartilhadas pelos testes (intenções e ledgers em memória).

`open_ledger()` registra o ledger aberto; a fixture autouse
`close_open_ledgers` (conftest) fecha todos ao fim de cada teste, então
`-W error::ResourceWarning` continua limpo.
"""

from decimal import Decimal

from trader.ledger import Ledger
from trader.models import SOLANA_MINTS
from trader.models.intent import IntentSide, TradeIntent
from trader.paths import PROJECT_ROOT

USDC = SOLANA_MINTS.get_by_symbol("USDC").mint
SOL = SOLANA_MINTS.get_by_symbol("SOL").mint
BONK = SOLANA_MINTS.get_by_symbol("BONK").mint

_OPEN_LEDGERS: list[Ledger] = []


def make_intent(
    side: IntentSide = IntentSide.BUY,
    notional: str | None = "10",
    spend_amount: str = "10",
    key: str | None = None,
    **overrides,
) -> TradeIntent:
    """Compra de 10 USDC -> SOL em `paper:SOL-USDC`; `overrides` troca campos."""
    if key is not None:
        overrides["idempotency_key"] = key
    return TradeIntent(
        source=overrides.pop("source", "test"),
        account=overrides.pop("account", "paper:SOL-USDC"),
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


def memory_gateway(policy=None):
    """`TradeGateway.in_memory()` com o ledger fechado ao fim do teste."""
    from trader.execution import TradeGateway

    gateway = TradeGateway.in_memory(policy)
    _OPEN_LEDGERS.append(gateway.ledger)
    return gateway


def close_open_ledgers() -> None:
    while _OPEN_LEDGERS:
        _OPEN_LEDGERS.pop().close()


def example_spec(name: str) -> str:
    """Caminho absoluto de `docs/examples/spec-<name>.json` (os testes mudam o cwd)."""
    return str(PROJECT_ROOT / "docs" / "examples" / f"spec-{name}.json")


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


def bonk_quote():
    """A quote dos mocks da Jupiter: 50 USDC -> 50 BONK, impacto de 0.5%.

    `priceImpactPct` é fração (0.005 == 0.5%), não percentual.
    """
    from trader.providers.jupiter.jupiter_data import JupiterQuoteResponse

    return JupiterQuoteResponse.single_route(
        USDC, 50_000_000, BONK, 5_000_000, price_impact_pct="0.005"
    )


def mock_provider(**attrs):
    """`AsyncMock(spec=AsyncJupiterProvider)` com os fatos do local de execução.

    Um mock com spec devolve Mock para propriedades; a conta faz contas com
    `native_fee_reserve`, então ela precisa ser um Decimal de verdade.
    """
    from unittest.mock import AsyncMock

    from trader.providers.jupiter.async_jupiter_svc import AsyncJupiterProvider

    provider = AsyncMock(spec=AsyncJupiterProvider)
    provider.native_fee_reserve = Decimal("0.02")
    for name, value in attrs.items():
        setattr(provider, name, value)
    return provider


class StubStrategy:
    """Estratégia mínima para testes do bot e do backtest (o `Strategy` do bot).

    Subclasses só implementam `on_market_refresh`; o resto não faz nada.
    """

    def __init__(self):
        import random
        from datetime import datetime

        self.clock = datetime.now
        self.rng = random.Random()

    def on_market_refresh(self, price, balance, current_position):
        return None

    def warmup(self):
        from trader.models import Interval

        return Interval.SECOND_15, 100

    def setup(self, ticker_history):
        return None

    def resume(self, last_exit_at, opened_at, last_exit_price=None):
        return None

    def set_clock(self, clock):
        self.clock = clock

    def seed(self, seed):
        import random

        self.rng = random.Random(seed)
