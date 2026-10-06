import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from unittest import mock
from unittest.mock import AsyncMock

import pytest
from factories import Inbox, StubStrategy, make_order, memory_gateway, served

from trader.backtest import Tick
from trader.backtest.replay import ReplayQuoteClient
from trader.execution.market import JupiterMarketData
from trader.execution.market.jupiter.async_jupiter_client import AsyncJupiterClient
from trader.execution.models.intent import IntentSide, IntentStatus
from trader.execution.trade.trading_service.service import TradeService
from trader.execution.trade.venues.paper import SimulatedWallet, paper_provider
from trader.shared.models import (
    SOLANA_MINTS,
    Interval,
    OrderSide,
    OrderSignal,
    Position,
)
from trader.shared.notification import NotificationService
from trader.shared.trading_service.protocol import (
    BucketSnapshot,
    BucketStatus,
    OrderReply,
    PriceUnavailableError,
    TradeServiceError,
)
from trader.strategy.bot.async_websocket_bot import (
    AsyncWebsocketTradingBot,
    format_price,
)
from trader.strategy.bot.config import BotConfig

ONE = Decimal(1)

USDC = SOLANA_MINTS.get_by_symbol("USDC")
BONK = SOLANA_MINTS.get_by_symbol("BONK")
# a spec do bucket no trade-runner: o orçamento é a carteira inteira
BONK_SPEC = {"symbol": "BONK-USDC", "budget_usd": 100, "max_loss_usd": 100}


class FakeStrategy(StubStrategy):
    def __init__(self):
        super().__init__()
        self.count = 0

    def on_market_refresh(
        self,
        price: Decimal,
        balance: Decimal,
        current_position: Position | None,
        quote_usd: Decimal | None = Decimal(1),
    ) -> OrderSignal | None:
        if self.count == 2:
            # encerra o bot após uma compra e uma venda
            raise KeyboardInterrupt()

        self.count += 1
        if current_position:
            return OrderSignal(OrderSide.SELL, current_position.entry_order.quantity)
        else:
            return OrderSignal(OrderSide.BUY, balance * Decimal("0.5") / price)


def _market_client():
    # o preço vem por um cliente próprio (websocket); swaps usam outro (HTTP)
    client = AsyncMock(spec=AsyncJupiterClient)
    client.get_candles = AsyncMock(return_value=[])
    client.get_price = AsyncMock(return_value=Decimal("1.0"))
    return client


async def test_a_buy_and_a_sell_go_all_the_way_to_the_ledger(mock_sleep):
    # bot -> TradeService -> gateway -> carteira paper, com quotes sintéticas
    strategy = FakeStrategy()
    quotes = ReplayQuoteClient(USDC, Decimal(0))
    quotes.tick = Tick(datetime(2026, 9, 1, 12, 0), Decimal("1"))
    wallet = SimulatedWallet(initial={"USDC": Decimal("100"), "SOL": Decimal("1")})
    gateway = memory_gateway()
    service = TradeService(paper_provider(wallet, jupiter_client=quotes), gateway)
    market_client = _market_client()
    async with served(service, **BONK_SPEC) as trader:
        bot = AsyncWebsocketTradingBot(
            BotConfig(
                name="e2e",
                symbol="BONK-USDC",
                strategy=strategy,
                market=JupiterMarketData(market_client),
                trader=trader,
                notifier=NotificationService(),
            )
        )
        await bot.arun()

    assert strategy.count == 2
    records = gateway.ledger.list_intents()
    assert [(r.intent.side, r.status) for r in reversed(records)] == [
        (IntentSide.BUY, IntentStatus.EXECUTED),
        (IntentSide.SELL, IntentStatus.EXECUTED),
    ]
    assert all(r.order_json for r in records)
    assert wallet.balance(BONK.mint) == 0  # vendeu tudo o que comprou
    market_client.aclose.assert_awaited_once()


async def test_fills_report_the_bucket_and_no_task_outlives_the_bot(
    mock_sleep,
):
    quotes = ReplayQuoteClient(USDC, Decimal(0))
    quotes.tick = Tick(datetime(2026, 9, 1, 12, 0), Decimal("1"))
    wallet = SimulatedWallet(initial={"USDC": Decimal("100"), "SOL": Decimal("1")})
    service = TradeService(
        paper_provider(wallet, jupiter_client=quotes), memory_gateway()
    )
    inbox = Inbox()
    async with served(service, **BONK_SPEC) as trader:
        bot = AsyncWebsocketTradingBot(
            BotConfig(
                name="e2e",
                symbol="BONK-USDC",
                strategy=FakeStrategy(),
                market=JupiterMarketData(_market_client()),
                trader=trader,
                notifier=inbox,
            )
        )
        await bot.arun()

    # nenhuma task sobra depois do bot
    assert asyncio.all_tasks() == {asyncio.current_task()}
    fills = [m for m in inbox.messages if m.startswith("Ordem executada")]
    # a compra mostra a posição marcada a mercado; a venda, o PnL realizado
    assert "aberto ~$" in fills[0] and "PNL líquido" in fills[0]
    assert "aberto" not in fills[1] and "PNL líquido" in fills[1]


class FakeTrader:
    """`TradeClient` falso: bucket fixo e respostas programadas."""

    def __init__(self, replies=(), status=BucketStatus.ACTIVE):
        self.replies = list(replies)
        self.status = status
        self.requests = []
        self.opened = False
        self.closed = 0

    async def open(self):
        self.opened = True

    async def bucket(self):
        return BucketSnapshot(
            "b", Decimal("100"), None, Decimal(0), ONE, status=self.status
        )

    async def submit(self, request):
        self.requests.append(request)
        return self.replies.pop(0) if self.replies else OrderReply.of_denial(("x",))

    async def aclose(self):
        self.closed += 1


def _market(prices=(Decimal("1"),)):
    market = AsyncMock(spec=JupiterMarketData)
    market.get_candles = AsyncMock(return_value=[])
    market.get_price = AsyncMock(side_effect=list(prices))
    return market


def _bot(market, strategy, trader=None, **config):
    return AsyncWebsocketTradingBot(
        BotConfig(
            name="name",
            symbol="BONK-USDC",
            strategy=strategy,
            market=market,
            trader=trader or FakeTrader(),
            notifier=NotificationService(),
            **config,
        )
    )


async def test_errors_back_off_exponentially_and_reset():
    market = _market(
        [
            Exception("ws down"),
            Exception("ws down"),
            Exception("ws down"),
            Decimal("1"),
            Exception("ws down"),
            KeyboardInterrupt(),
        ]
    )
    strategy = mock.Mock(wraps=StubStrategy())
    strategy.on_market_refresh.return_value = None
    bot = _bot(market, strategy)

    with mock.patch("asyncio.sleep") as sleep:
        await bot.arun()

    assert [c.args[0] for c in sleep.await_args_list] == [1.0, 2.0, 4.0, 1.0]


async def test_no_price_is_a_warning_without_traceback(caplog):
    # soak F10: um trade-runner recém-iniciado sem preço não é defeito do loop
    market = _market(
        [PriceUnavailableError("StalePriceError: sem preço"), KeyboardInterrupt()]
    )
    bot = _bot(market, StubStrategy())

    with mock.patch("asyncio.sleep") as sleep:
        await bot.arun()

    assert [c.args[0] for c in sleep.await_args_list] == [1.0]  # o backoff fica
    warned = [r for r in caplog.records if "Sem preço agora" in r.getMessage()]
    assert warned and warned[0].levelno == logging.WARNING
    assert warned[0].exc_info is None
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_a_fill_is_reported_without_a_pause():
    # soak F11 e A5: sem os 2 s depois do fill; o fill é reportado na hora
    inbox = Inbox()
    trader = FakeTrader([OrderReply.of_fill(make_order(output_mint=BONK.mint))])
    strategy = mock.Mock(wraps=StubStrategy())
    strategy.on_market_refresh.return_value = OrderSignal(OrderSide.BUY, ONE)
    bot = _bot(_market([ONE, KeyboardInterrupt()]), strategy, trader)
    bot.notification_service = inbox

    with mock.patch("asyncio.sleep") as sleep:
        await bot.arun()

    assert any(m.startswith("Ordem executada") for m in inbox.messages)
    sleep.assert_not_awaited()


async def test_stop_ends_the_loop():
    market = _market()
    market.get_price = AsyncMock(return_value=Decimal("1"))
    strategy = mock.Mock(wraps=StubStrategy())
    bot = _bot(market, strategy)
    strategy.on_market_refresh.side_effect = lambda *a, **k: bot.stop()

    await bot.arun()

    strategy.on_market_refresh.assert_called_once()


async def test_startup_opens_the_bucket_and_shutdown_closes_everything():
    market = _market([KeyboardInterrupt()])
    trader = FakeTrader()
    bot = _bot(market, mock.Mock(wraps=StubStrategy()), trader)

    await bot.arun()

    assert trader.opened
    assert trader.closed == 1
    market.aclose.assert_awaited_once()
    assert bot.is_running is False


async def test_cancellation_closes_and_propagates():
    market = _market([asyncio.CancelledError()])
    trader = FakeTrader()
    notifier = mock.Mock(spec=NotificationService)
    bot = _bot(market, mock.Mock(wraps=StubStrategy()), trader)
    bot.notification_service = notifier

    with pytest.raises(asyncio.CancelledError):
        await bot.arun()

    market.aclose.assert_awaited_once()
    assert trader.closed == 1
    notifier.send_message.assert_called_with("Bot interrompido pelo usuário")
    # o encerramento espera o envio pendente da mensagem acima
    notifier.aclose.assert_awaited_once()


async def test_shutdown_survives_close_errors():
    market = _market([KeyboardInterrupt()])
    market.aclose = AsyncMock(side_effect=RuntimeError("already closed"))
    trader = FakeTrader()
    bot = _bot(market, mock.Mock(wraps=StubStrategy()), trader)

    await bot.arun()  # não propaga o erro de fechamento
    assert trader.closed == 1  # o trader fecha mesmo com erro no market


def _buy_strategy():
    strategy = mock.Mock(wraps=StubStrategy())
    strategy.on_market_refresh.return_value = OrderSignal(
        OrderSide.BUY, Decimal("1"), rationale="porque sim"
    )
    return strategy


def _snapshot(status=BucketStatus.ACTIVE):
    return BucketSnapshot("b", Decimal("100"), None, Decimal(0), ONE, status=status)


async def test_denied_order_pauses_orders_but_not_the_strategy():
    strategy = _buy_strategy()
    trader = FakeTrader()  # nega todas
    bot = _bot(_market(), strategy, trader)
    clock = [0.0]
    bot.monotonic = lambda: clock[0]

    assert await bot.process_market_data(Decimal("1"), _snapshot()) is None
    # dentro do cooldown: a estratégia roda, mas nenhuma ordem é tentada
    for _ in range(3):
        assert await bot.process_market_data(Decimal("1"), _snapshot()) is None
    assert len(trader.requests) == 1
    assert strategy.on_market_refresh.call_count == 4
    assert trader.requests[0].rationale == "porque sim"

    clock[0] += bot.denial_cooldown
    await bot.process_market_data(Decimal("1"), _snapshot())
    assert len(trader.requests) == 2


async def test_error_reply_goes_through_the_backoff():
    trader = FakeTrader([OrderReply.of_error("RuntimeError: rpc caiu")])
    bot = _bot(_market(), _buy_strategy(), trader)
    with pytest.raises(TradeServiceError, match="rpc caiu"):
        await bot.process_market_data(Decimal("1"), _snapshot())


async def test_retiring_bucket_stops_the_bot():
    strategy = _buy_strategy()
    market = _market([Decimal("1"), Decimal("1")])
    bot = _bot(market, strategy, FakeTrader(status=BucketStatus.RETIRING))

    await bot.arun()

    strategy.on_market_refresh.assert_not_called()
    assert market.get_price.await_count == 1


async def test_warmup_and_tick_callback():
    class Warm(StubStrategy):
        def warmup(self):
            return Interval.HOUR_1, 24

        def on_market_refresh(
            self,
            price,
            balance,
            current_position,
            quote_usd: Decimal | None = Decimal(1),
        ):
            raise KeyboardInterrupt()

    market = _market([Decimal("2")])
    ticks = []
    bot = _bot(market, Warm(), on_tick=lambda ts, price, quote_usd: ticks.append(price))

    await bot.arun()

    market.get_candles.assert_awaited_once_with(BONK.mint, Interval.HOUR_1, 24)
    assert ticks == [Decimal("2")]


async def test_a_failed_startup_backs_off_and_opens_the_bucket_once():
    market = _market([Decimal("1"), KeyboardInterrupt()])
    market.get_candles = AsyncMock(side_effect=[OSError("429 nos candles"), []])
    trader = FakeTrader()
    opens = []
    trader.open = AsyncMock(side_effect=lambda: opens.append(1))
    strategy = mock.Mock(wraps=StubStrategy())
    strategy.on_market_refresh.return_value = None
    bot = _bot(market, strategy, trader)

    with mock.patch("asyncio.sleep") as sleep:
        await bot.arun()  # antes: o erro de startup encerrava o bot

    assert len(opens) == 1
    assert market.get_candles.await_count == 2
    assert [c.args[0] for c in sleep.await_args_list] == [1.0]
    strategy.on_market_refresh.assert_called_once()


async def test_startup_resumes_a_strategy_that_keeps_state():
    class Resuming(FakeStrategy):
        def resume(self, last_exit_at, opened_at, last_exit_price=None):
            self.resumed = (last_exit_at, opened_at, last_exit_price)

    strategy = Resuming()
    bot = _bot(_market([KeyboardInterrupt()]), strategy)

    await bot.arun()

    assert strategy.resumed == (None, None, None)  # o FakeTrader não tem histórico


async def test_a_failed_first_bucket_read_still_resumes_later(mock_sleep):
    # T4: com `_opened` já marcado, um erro no primeiro `bucket()` pulava o
    # `resume()` para sempre (cooldown, rearme e `ttl_days` perdidos)
    class Resuming(FakeStrategy):
        resumed = False

        def resume(self, last_exit_at, opened_at, last_exit_price=None):
            self.resumed = True

    class FlakyTrader(FakeTrader):
        reads = 0

        async def bucket(self):
            self.reads += 1
            if self.reads == 1:
                raise OSError("saldo indisponível")
            return await super().bucket()

    strategy = Resuming()
    bot = _bot(_market([KeyboardInterrupt()]), strategy, FlakyTrader())

    await bot.arun()

    assert strategy.resumed


async def test_repeated_startup_failures_alert_the_owner_once():
    from trader.strategy.bot import async_websocket_bot as bot_module

    market = _market([KeyboardInterrupt()])
    failures = [OSError("datapi fora")] * (bot_module.STARTUP_ALERT_AFTER + 1)
    market.get_candles = AsyncMock(side_effect=[*failures, []])
    notifier = mock.Mock(spec=NotificationService)
    bot = _bot(market, mock.Mock(wraps=StubStrategy()))
    bot.notification_service = notifier

    with mock.patch("asyncio.sleep"):
        await bot.arun()

    alerts = [
        c for c in notifier.send_message.call_args_list if "não consegue" in str(c)
    ]
    assert len(alerts) == 1


def test_prices_are_logged_with_significant_digits():
    assert format_price(Decimal("121.48")) == "121.48000"
    assert format_price(Decimal("0.000606123456")) == "0.00060612346"
    assert format_price(Decimal("12345678.9")) == "12345679"
    assert format_price(Decimal(0)) == "0.00000000"


async def test_the_ticker_is_logged_once_per_bar(caplog):
    # soak F3: uma linha por tick (1/s) enchia o arquivo; agora uma por barra
    prices = [Decimal("1")] * 4
    bot = _bot(_market(prices), StubStrategy())  # barras de 15s
    clock = iter([100.0, 101.0, 114.9, 115.0])
    bot.wall_clock = lambda: next(clock)
    await bot._startup()

    with caplog.at_level(logging.DEBUG, logger="bot"):
        for _ in prices:
            await bot._tick()

    lines = [r for r in caplog.records if "BONK-USDC" in r.getMessage()]
    assert len(lines) == 2  # barras 6 (100-114.9) e 7 (115)
