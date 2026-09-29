from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from factories import memory_gateway

from trader.backtest import (
    Backtester,
    Tick,
    TickRecorder,
    load_ticks,
    ticks_from_candles,
)
from trader.execution import KillSwitch, TradeGateway
from trader.models import OrderSide, OrderSignal, TickerData
from trader.models.intent import IntentStatus
from trader.trading_strategy import (
    RandomStrategy,
    StrategyComposer,
    TradingStrategy,
    TrailingStopLossStrategy,
    WeightedMovingAverageStrategy,
)

START = datetime(2026, 9, 1, 12, 0)


def _ticks(prices, step_seconds=60):
    return [
        Tick(START + timedelta(seconds=i * step_seconds), Decimal(str(p)))
        for i, p in enumerate(prices)
    ]


class BuyThenSell(TradingStrategy):
    """Compra no primeiro tick e vende no tick `sell_at`."""

    def __init__(self, sell_at):
        super().__init__()
        self.sell_at = sell_at
        self.tick = 0

    def on_market_refresh(self, price, spread, balance, current_position):
        self.tick += 1
        if not current_position and self.tick == 1:
            return OrderSignal(OrderSide.BUY, balance / price)
        if current_position and self.tick == self.sell_at:
            return OrderSignal(OrderSide.SELL, current_position.entry_order.quantity)
        return None


async def _run(strategy, prices, **kwargs):
    return await Backtester(
        strategy, "SOL-USDC", _ticks(prices), Decimal("100"), **kwargs
    ).run()


class TestBacktester:
    async def test_round_trip_pnl_with_fees(self):
        result = await _run(
            BuyThenSell(sell_at=3), [100, 105, 110], fee_bps=Decimal("0")
        )

        assert [t.side for t in result.trades] == [OrderSide.BUY, OrderSide.SELL]
        assert result.trades[0].price == Decimal("100")
        assert result.trades[1].realized_pnl == Decimal("10")
        assert result.final_equity == Decimal("110")
        assert result.return_pct == Decimal("10")
        assert not result.open_position

    async def test_replays_through_a_gateway_that_ignores_the_live_halt(
        self, monkeypatch
    ):
        KillSwitch().activate("bot ao vivo parado")
        gateway = memory_gateway()  # fechado pela fixture, não pelo replay
        gateway.close = lambda: None  # para o teste ler o ledger depois
        monkeypatch.setattr(TradeGateway, "in_memory", lambda policy=None: gateway)

        result = await _run(BuyThenSell(sell_at=3), [100, 105, 110])

        assert len(result.trades) == 2
        records = gateway.ledger.list_intents()
        assert [r.status for r in records] == [IntentStatus.EXECUTED] * 2
        assert all(r.order_json for r in records)

    async def test_fees_reduce_result(self):
        free = await _run(BuyThenSell(3), [100, 105, 110], fee_bps=Decimal("0"))
        paid = await _run(BuyThenSell(3), [100, 105, 110], fee_bps=Decimal("50"))
        assert paid.final_equity < free.final_equity
        # 0.5% na compra e 0.5% na venda
        assert paid.final_equity == Decimal("110") * Decimal("0.995") ** 2

    async def test_drawdown_and_open_position(self):
        result = await _run(
            BuyThenSell(sell_at=99), [100, 80, 90], fee_bps=Decimal("0")
        )
        assert result.open_position
        assert result.max_drawdown_pct == Decimal("20")
        assert result.final_equity == Decimal("90")
        assert result.realized_pnl == Decimal("0")

    async def test_is_deterministic(self):
        prices = [100 + (i % 7) - (i % 3) for i in range(300)]

        def strategy():
            return RandomStrategy(sell_chance=10, buy_chance=10)

        first = await _run(strategy(), prices, seed=42)
        second = await _run(strategy(), prices, seed=42)
        other_seed = await _run(strategy(), prices, seed=7)

        assert first.trades and first == second
        assert first.trades != other_seed.trades

    async def test_time_based_strategies_use_tick_clock(self):
        # WMA com período de 60s: com ticks de 60s cada tick entra no histórico.
        # Se usasse o relógio real, os ticks (reproduzidos em ms) cairiam todos
        # no mesmo período e a estratégia nunca aqueceria.
        prices = [100 - i for i in range(10)] + [90 + i for i in range(10)]
        composer = StrategyComposer(
            buy_strategies=[
                WeightedMovingAverageStrategy(short_window=2, long_window=4, period=60)
            ],
            sell_strategies=[TrailingStopLossStrategy(stop_loss_percent="50")],
        )
        result = await _run(composer, prices)
        assert result.trades
        assert result.trades[0].side == OrderSide.BUY

    async def test_rejected_signals_are_counted(self):
        class SellWithoutPosition(TradingStrategy):
            def on_market_refresh(self, price, spread, balance, current_position):
                return OrderSignal(OrderSide.SELL, Decimal("1"))

        result = await _run(SellWithoutPosition(), [100, 101])
        assert result.rejected_signals == 2
        assert result.trades == []

    def test_requires_stablecoin_input(self):
        with pytest.raises(ValueError, match="stablecoin"):
            Backtester(BuyThenSell(2), "USDC-SOL", _ticks([1]), Decimal("1"))

    def test_requires_ticks(self):
        with pytest.raises(ValueError, match="tick"):
            Backtester(BuyThenSell(2), "SOL-USDC", [], Decimal("1"))

    async def test_summary(self):
        result = await _run(BuyThenSell(3), [100, 105, 110])
        text = result.summary()
        assert "SOL-USDC" in text
        assert "trades: 2 (1 fechados, win rate 100.0%)" in text


class TestTicks:
    def test_record_and_load_roundtrip(self, tmp_path):
        path = tmp_path / "ticks" / "sol.csv"
        recorder = TickRecorder(path)
        for tick in reversed(_ticks([1.5, 2, 3])):
            recorder.record(tick.timestamp, tick.price)
        recorder.close()

        loaded = load_ticks(path)
        assert loaded == _ticks([1.5, 2, 3])  # ordenado por tempo

    def test_recorder_appends(self, tmp_path):
        path = tmp_path / "t.csv"
        for price in ("1", "2"):
            recorder = TickRecorder(path)
            recorder.record(START, Decimal(price))
            recorder.close()
        assert [t.price for t in load_ticks(path)] == [Decimal("1"), Decimal("2")]

    def test_load_skips_comments_and_reports_bad_lines(self, tmp_path):
        path = tmp_path / "t.csv"
        path.write_text(
            "# timestamp,price\n2026-09-01T12:00:00,1\n\nnot-a-date,2\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match=":4:"):
            load_ticks(path)

    def test_ticks_from_candles(self):
        candle = TickerData(
            buy=Decimal("1"),
            timestamp=START,
            high=Decimal("3"),
            last=Decimal("2"),
            low=Decimal("1"),
            open=Decimal("1"),
            pair="x",
            sell=Decimal("1"),
            vol=Decimal("0"),
        )
        later = replace(
            candle, timestamp=START + timedelta(minutes=1), last=Decimal("4")
        )
        assert ticks_from_candles([later, candle]) == [
            Tick(START, Decimal("2")),
            Tick(START + timedelta(minutes=1), Decimal("4")),
        ]


class TestStrategyDeterminism:
    def test_random_strategy_seed(self):
        def signals(seed):
            strategy = RandomStrategy(sell_chance=50, buy_chance=50, seed=seed)
            return [
                strategy.on_market_refresh(Decimal("1"), None, Decimal("1"), None)
                for _ in range(50)
            ]

        assert signals(1) == signals(1)
        assert signals(1) != signals(2)

    def test_composer_propagates_clock_and_seed(self):
        child = RandomStrategy(sell_chance=1, buy_chance=1)
        composer = StrategyComposer(buy_strategies=[child])
        composer.set_clock(lambda: START)
        composer.seed(3)
        assert child.clock() == START
        first = child.rng.random()
        composer.seed(3)
        assert child.rng.random() == first
