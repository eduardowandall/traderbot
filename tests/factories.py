"""Fábricas compartilhadas pelos testes (intenções e ledgers em memória).

`open_ledger()` registra o ledger aberto; a fixture autouse
`close_open_ledgers` (conftest) fecha todos ao fim de cada teste, então
`-W error::ResourceWarning` continua limpo.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

from trader.execution.models.intent import IntentSide, TradeIntent
from trader.execution.trade.ledger import Ledger
from trader.shared.models import SOLANA_MINTS
from trader.shared.paths import PROJECT_ROOT
from trader.shared.spec.terms import SpecTerms
from trader.strategy.spec.models import StrategySpec

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


def make_order(
    side: str = "buy",
    quantity: str = "0.1",
    price: str = "100",
    timestamp: datetime | None = None,
    output_mint: str = SOL,
):
    """Fill de USDC <-> token com valores nativos (custo = quantidade x preço)."""
    from trader.shared.models import Order, OrderSide

    return Order(
        order_id=f"{side}-{price}-{timestamp}",
        input_mint=USDC,
        output_mint=output_mint,
        quantity=Decimal(quantity),
        price=Decimal(price),
        side=OrderSide(side),
        timestamp=timestamp or datetime(2026, 9, 24, 12, 0, tzinfo=UTC),
        fill_price=Decimal(price),
        quote_amount=Decimal(quantity) * Decimal(price),
        quote_usd=Decimal("1"),
    )


def record_executed(ledger: Ledger, intent: TradeIntent, order=None, realized=None):
    """Grava `intent` como executada (e a ordem, com PnL em vendas)."""
    from trader.execution.models.intent import PolicyDecision
    from trader.shared.models import SwapResult

    ledger.record_intent(intent, PolicyDecision(True))
    ledger.mark_executed(
        intent.intent_id,
        SwapResult(
            f"sig-{intent.intent_id}", intent.spend_mint, intent.receive_mint, 1, 2
        ),
    )
    if order is not None:
        realized = None if realized is None else Decimal(realized)
        ledger.attach_order(intent.intent_id, order, realized)


def executed_leg(
    ledger: Ledger, account: str, side: str, at: datetime, realized=None, **order
):
    """Uma perna executada de `account` (SOL-USDC) criada em `at`."""
    order.setdefault("timestamp", at)
    fill = make_order(side, **order)
    buy = side == "buy"
    intent = make_intent(
        IntentSide.BUY if buy else IntentSide.SELL,
        account=account,
        created_at=at,
        spend_mint=USDC if buy else fill.output_mint,
        receive_mint=fill.output_mint if buy else USDC,
        quantity=fill.quantity,
        price=fill.price,
        closes_position=None if buy else True,
    )
    record_executed(ledger, intent, fill, realized)
    return intent


def open_ledger() -> Ledger:
    """Ledger em memória, fechado automaticamente ao fim do teste."""
    ledger = Ledger()
    _OPEN_LEDGERS.append(ledger)
    return ledger


def memory_gateway(policy=None):
    """`TradeGateway.in_memory()` com o ledger fechado ao fim do teste."""
    from trader.execution.trade.gateway import TradeGateway

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


def terms_of(spec: dict) -> SpecTerms:
    """Os termos (o que o `hello` manda) de uma spec em dict JSON."""
    return StrategySpec.model_validate(spec).terms()


class PriceTable:
    """Price API falsa: os preços USD de um dict (lido a cada pedido)."""

    def __init__(self, prices: dict[str, Decimal] | None = None):
        self.prices = prices if prices is not None else {}
        self.calls = 0

    async def get_usd_prices(self, mints):
        self.calls += 1
        return {m: self.prices[m] for m in mints if m in self.prices}

    async def aclose(self):
        return None


class NoCandles:
    """`CandleSource` vazio (o trade-runner exige um)."""

    async def get_candles(self, mint, interval, candle_qty):
        return []

    async def aclose(self):
        return None


def trade_runner(service, limits=None, prices=None, candles=None, **kwargs):
    """Um `TradeRunner` de paper com hub sobre `PriceTable(prices)`, sem rede."""
    from trader.execution.market.hub import PriceHub
    from trader.execution.runner import TradeRunner
    from trader.shared.spec.validate import SpecLimits

    return TradeRunner(
        service,
        "paper",
        limits or SpecLimits(Decimal(1000)),
        hub=PriceHub(PriceTable(prices), stream=None),  # type: ignore[arg-type]
        candles=candles or NoCandles(),
        **kwargs,
    )


@asynccontextmanager
async def served(service, terms: SpecTerms | None = None, **spec_overrides):
    """O serviço atrás de um `TradeRunner` local: o `RemoteTradeClient` da spec.

    Sem `terms`, os de `make_spec(ttl_days=5, **spec_overrides)` (paper recusa
    prazos de mais de 30 dias). O cliente sai fechado (o bot abre e fecha); o
    serviço fica com quem o criou.
    """
    from trader.strategy.trading_service.remote import RemoteTradeClient

    spec = make_spec(ttl_days=5, expires_at=None) | spec_overrides

    runner = trade_runner(service)
    async with await runner.start() as server:
        port = server.sockets[0].getsockname()[1]
        yield RemoteTradeClient(
            "127.0.0.1", port, runner.token, terms or terms_of(spec)
        )


def bonk_quote():
    """A quote dos mocks da Jupiter: 50 USDC -> 50 BONK, impacto de 0.5%.

    `priceImpactPct` é fração (0.005 == 0.5%), não percentual.
    """
    from trader.execution.market.jupiter.jupiter_data import JupiterQuoteResponse

    return JupiterQuoteResponse.single_route(
        USDC, 50_000_000, BONK, 5_000_000, price_impact_pct="0.005"
    )


def mock_provider(**attrs):
    """`AsyncMock(spec=AsyncJupiterProvider)` com os fatos do local de execução.

    Um mock com spec devolve Mock para propriedades e métodos; o que a conta
    usa em contas devolve tipos de verdade: `native_fee_reserve` (Decimal) e
    `fetch_swap_costs` (custos desconhecidos, como o provider sem leitura).
    """
    from unittest.mock import AsyncMock

    from trader.execution.trade.venues.jupiter.async_jupiter_svc import (
        AsyncJupiterProvider,
    )
    from trader.shared.models.costs import QUOTE, TradeCosts

    provider = AsyncMock(spec=AsyncJupiterProvider)
    provider.native_fee_reserve = Decimal("0.02")
    provider.fetch_swap_costs.return_value = TradeCosts(source=QUOTE)
    provider.fetch_failed_fees.return_value = 0
    # a leitura direta só confirma a da carteira (A6 F1): nada a mais
    provider.token_balance.return_value = Decimal("0")
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

    def on_market_refresh(
        self, price, balance, current_position, quote_usd: Decimal | None = Decimal(1)
    ):
        return None

    def warmup(self):
        from trader.shared.models import Interval

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


def inspection_passes(rpc, lamports: int = 10**9):
    """Um RPC falso cuja inspeção da transação passa (B7).

    Carteira sem contas de token e uma simulação que não gasta nada: a
    transação de teste só precisa chegar ao envio.
    """
    from unittest.mock import AsyncMock

    from trader.execution.trade.venues.jupiter.tx_inspection import WalletState

    rpc.wallet_state = AsyncMock(return_value=WalletState(lamports, {}))
    rpc.simulate_transaction = AsyncMock(return_value=simulation(lamports))
    return rpc


def events_of(ledger: Ledger, type_: str) -> list[dict]:
    """Os payloads dos eventos de um tipo, na ordem."""
    import json

    rows = ledger.conn.execute(
        "SELECT payload FROM events WHERE type = ? ORDER BY id", (type_,)
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def signs(rpc, last_valid_block_height: int = 1_000):
    """Um RPC falso que "assina" devolvendo a própria transação (A3: `SignedTx`)."""
    from unittest.mock import AsyncMock

    from trader.execution.trade.venues.jupiter.async_rpc_client import SignedTx

    rpc.sign_transaction = AsyncMock(
        side_effect=lambda tx, keypair: SignedTx(tx, last_valid_block_height)
    )
    return rpc


def simulation(lamports: int = 10**9, tokens=()):
    """Resposta de simulação com a carteira e as contas de token pedidas."""
    from types import SimpleNamespace

    accounts = [SimpleNamespace(lamports=lamports)]
    accounts += [SimpleNamespace(data=data) for data in tokens]
    return SimpleNamespace(value=SimpleNamespace(accounts=accounts, err=None))


class Inbox:
    """`Notifier` que guarda as mensagens (sem Telegram)."""

    def __init__(self):
        self.messages: list[str] = []

    def send_message(self, message: str) -> None:
        self.messages.append(message)

    async def aclose(self) -> None:
        return None
