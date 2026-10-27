"""A29: o bot espera a janela da recusa (até 1 h) em vez dos 30 s."""

import logging
from decimal import Decimal
from unittest import mock
from unittest.mock import AsyncMock

from factories import StubStrategy

from trader.shared.market import MarketData
from trader.shared.models import OrderSide, OrderSignal
from trader.shared.notification import NotificationService
from trader.shared.trading_service.protocol import (
    BucketSnapshot,
    OrderReply,
)
from trader.shared.trading_service.wire import reply_from_dict
from trader.strategy.bot.config import BotConfig
from trader.strategy.bot.loop import MAX_DENIAL_WAIT, TradingBot, wait_text

ONE = Decimal(1)
SNAPSHOT = BucketSnapshot("b", Decimal(100), None, Decimal(0), ONE)


class Trader:
    """Responde com `reply` a toda ordem e guarda os pedidos."""

    def __init__(self, reply: OrderReply):
        self.reply = reply
        self.requests = []

    async def submit(self, request):
        self.requests.append(request)
        return self.reply


def _bot(trader, side=OrderSide.BUY):
    strategy = mock.Mock(wraps=StubStrategy())
    strategy.on_market_refresh.return_value = OrderSignal(side, ONE)
    bot = TradingBot(
        BotConfig(
            name="name",
            symbol="BONK-USDC",
            strategy=strategy,
            market=AsyncMock(spec=MarketData),
            trader=trader,  # type: ignore[arg-type]
            notifier=NotificationService(),
        )
    )
    clock = [0.0]
    bot.monotonic = lambda: clock[0]
    return bot, clock


async def _orders_at(bot, clock, trader, *times: float) -> list[int]:
    """Quantos pedidos o trader viu depois de um tick em cada instante."""
    seen = []
    for at in times:
        clock[0] = at
        await bot.process_market_data(ONE, SNAPSHOT)
        seen.append(len(trader.requests))
    return seen


async def test_no_order_before_the_window_frees():
    trader = Trader(OrderReply.of_denial(("limite diário",), 600.0))
    bot, clock = _bot(trader)

    seen = await _orders_at(bot, clock, trader, 0, 30, 300, 599, 600)

    # os 30 s antigos não valem mais; aos 600 s pergunta de novo
    assert seen == [1, 1, 1, 1, 2]


async def test_the_wait_is_capped_at_an_hour_then_it_asks_again():
    trader = Trader(OrderReply.of_denial(("limite diário",), 20 * 3600.0))
    bot, clock = _bot(trader)

    seen = await _orders_at(bot, clock, trader, 0, 3599, MAX_DENIAL_WAIT)

    assert seen == [1, 1, 2]


async def test_a_denial_without_a_window_keeps_the_30s():
    trader = Trader(OrderReply.of_denial(("acima do limite",)))
    bot, clock = _bot(trader)

    seen = await _orders_at(bot, clock, trader, 0, 29, bot.denial_cooldown)

    assert seen == [1, 1, 2]


async def test_a_reply_from_an_older_serve_keeps_the_30s():
    older = {"status": "denied", "order": None, "reasons": ["x"], "error": None}
    trader = Trader(reply_from_dict(older))
    bot, clock = _bot(trader)

    assert await _orders_at(bot, clock, trader, 0, 29, 30) == [1, 1, 2]


async def test_a_buy_waiting_its_window_never_holds_a_sell():
    trader = Trader(OrderReply.of_denial(("limite diário",), 3600.0))
    bot, clock = _bot(trader)
    await _orders_at(bot, clock, trader, 0)

    bot.strategy.on_market_refresh.return_value = OrderSignal(OrderSide.SELL, ONE)  # type: ignore[attr-defined]
    trader.reply = OrderReply.of_rejection("saldo")
    assert await _orders_at(bot, clock, trader, 10) == [2]
    assert trader.requests[-1].side == OrderSide.SELL


async def test_the_log_says_the_real_wait(caplog):
    trader = Trader(OrderReply.of_denial(("limite diário",), 7 * 3600.0))
    bot, clock = _bot(trader)

    with caplog.at_level(logging.WARNING):
        await _orders_at(bot, clock, trader, 0)

    assert "(novas ordens em 1h00)" in caplog.text


def test_wait_text():
    assert wait_text(30) == "30s"
    assert wait_text(600) == "10 min"
    assert wait_text(3900) == "1h05"
