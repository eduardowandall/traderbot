"""A28: uma queda de preço ou da conferência das perps avisa uma vez, com
posição aberta, e uma vez quando passa."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

from factories import open_ledger, trade_runner

from trader.backtest import Tick
from trader.backtest.replay import ReplayQuoteClient
from trader.execution.notification.outages import Outage
from trader.execution.trade.gateway import TradeGateway
from trader.execution.trade.policy import Policy
from trader.execution.trade.trading_service.service import TradeService
from trader.execution.trade.venues.paper import SimulatedWallet, paper_provider
from trader.execution.trade.venues.spot import SpotVenue
from trader.shared.models import SOLANA_MINTS, OrderSide
from trader.shared.trading_service.protocol import OrderRequest

USDC = SOLANA_MINTS.get_by_symbol("USDC")
SOL = SOLANA_MINTS.get_by_symbol("SOL")
BUCKET = "strategy:x"
T0 = datetime(2026, 10, 12, 19, 13, tzinfo=UTC)
LOOSE = Policy(
    max_trade_usd=Decimal(1000),
    max_daily_notional_usd=Decimal(100000),
    max_trades_per_hour=100000,
)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Telegram:
    def __init__(self):
        self.messages: list[str] = []

    def send_message(self, message: str) -> None:
        self.messages.append(message)

    async def aclose(self) -> None:
        return None


# --- o alarme -----------------------------------------------------------------------


def _outage():
    clock, telegram = Clock(), Telegram()
    return (
        Outage("paper: preços", telegram, after=120, monotonic=clock),
        clock,
        telegram,
    )


def test_one_alert_per_outage_after_two_minutes_and_one_on_recovery():
    outage, clock, telegram = _outage()
    outage.failing(True, "sem preço")
    clock.now += 119
    outage.failing(True, "sem preço")
    assert telegram.messages == []  # ainda não: 2 min

    clock.now += 1
    outage.failing(True, "sem preço")
    clock.now += 600
    outage.failing(True, "sem preço")  # a mesma queda: nada de novo
    assert telegram.messages == [
        "[!] paper: preços fora há 120s com posição aberta: sem preço"
    ]

    clock.now += 10
    outage.ok()
    outage.ok()
    assert telegram.messages[1:] == ["paper: preços de volta depois de 730s"]

    # uma queda nova conta do zero
    outage.failing(True, "de novo")
    clock.now += 60
    outage.failing(True, "de novo")
    assert len(telegram.messages) == 2


def test_no_alert_without_an_open_position_and_no_recovery_message_then():
    outage, clock, telegram = _outage()
    for _ in range(10):
        outage.failing(False, "sem preço")
        clock.now += 60
    outage.ok()
    assert telegram.messages == []


def test_a_position_opened_during_a_long_outage_alerts_at_once():
    outage, clock, telegram = _outage()
    outage.failing(False, "x")
    clock.now += 300
    outage.failing(True, "x")
    assert telegram.messages == ["[!] paper: preços fora há 300s com posição aberta: x"]


# --- no trade-runner ---------------------------------------------------------------


async def _runner(position: bool, prices: dict | None = None):
    """Um bucket SOL-USDC em paper; o hub lê `prices` (mude-o para a API cair)."""
    quotes = ReplayQuoteClient(USDC, Decimal(0))
    quotes.tick = Tick(T0, Decimal(100))
    wallet = SimulatedWallet(initial={"USDC": Decimal(100), "SOL": Decimal(1)})
    service = TradeService(
        SpotVenue(paper_provider(wallet, jupiter_client=quotes)),
        TradeGateway(open_ledger(), LOOSE, False),
        mode="paper",
    )
    telegram, clock = Telegram(), Clock()
    prices = prices if prices is not None else {SOL.mint: Decimal(100)}
    runner = trade_runner(service, prices=prices, notifier=telegram)
    runner.hub.monotonic = clock
    runner.price_outage.monotonic = clock
    runner.perps_outage.monotonic = clock
    await service.open_bucket(BUCKET, USDC.mint, SOL.mint, budget_usd=Decimal(50))
    if position:
        buy = OrderRequest(OrderSide.BUY, Decimal("0.2"), Decimal(100))
        assert (await service.submit_order(BUCKET, buy)).filled
    return runner, telegram, clock


async def _price_outage(runner, clock, prices: dict, minutes: int) -> None:
    """A Price API para de responder (DNS) e o hub fica sem preço novo."""
    prices.clear()
    for _ in range(minutes * 6):
        clock.now += 10
        await runner.check_prices()


async def test_prices_stale_with_an_open_position_alert_once_and_on_recovery():
    prices = {SOL.mint: Decimal(100)}
    runner, telegram, clock = await _runner(position=True, prices=prices)
    await runner.check_prices()  # fresco: nada
    try:
        await _price_outage(runner, clock, prices, minutes=5)
        assert len(telegram.messages) == 1
        assert telegram.messages[0].startswith(
            "[!] paper: preços fora há 120s com posição aberta: sem preço fresco (SOL"
        )

        prices[SOL.mint] = Decimal(101)  # a rede voltou
        await runner.check_prices()
        await runner.check_prices()
        assert len(telegram.messages) == 2
        assert telegram.messages[1].startswith("paper: preços de volta depois de")
    finally:
        await runner.service.aclose()
        runner.service.gateway.ledger.close()


async def test_prices_stale_without_a_position_send_nothing():
    prices = {SOL.mint: Decimal(100)}
    runner, telegram, clock = await _runner(position=False, prices=prices)
    try:
        await _price_outage(runner, clock, prices, minutes=5)
        assert telegram.messages == []
    finally:
        await runner.service.aclose()
        runner.service.gateway.ledger.close()


async def test_a_failing_perps_sweep_alerts_once_and_on_recovery():
    runner, telegram, clock = await _runner(position=True)
    service = runner.service
    service.check_perps = AsyncMock(side_effect=OSError("getaddrinfo failed"))  # type: ignore[method-assign]
    try:
        for _ in range(10):  # 5 min de varreduras falhando
            await runner.sweep()
            clock.now += 30
        assert telegram.messages == [
            "[!] paper: conferência das perps fora há 120s com posição aberta: "
            "getaddrinfo failed"
        ]
        service.check_perps = AsyncMock(return_value={})  # type: ignore[method-assign]
        await runner.sweep()
        await runner.sweep()
        assert telegram.messages[1] == (
            "paper: conferência das perps de volta depois de 300s"
        )
        assert len(telegram.messages) == 2
    finally:
        await service.aclose()
        service.gateway.ledger.close()
