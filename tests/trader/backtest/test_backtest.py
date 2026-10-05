from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from factories import StubStrategy, make_spec

from trader.backtest import (
    Backtester,
    Tick,
    TickRecorder,
    load_ticks,
)
from trader.shared.models import OrderSide, OrderSignal
from trader.strategy.spec.models import StrategySpec
from trader.strategy.spec.strategy import SpecStrategy

ONE = Decimal(1)
# a taxa vira lamports ao SOL do tick: arredonda abaixo de 1e-6 USD
LAMPORT_ROUNDING = Decimal("0.000001")

START = datetime(2026, 9, 1, 12, 0)


def _random_spec(buy: int, sell: int) -> SpecStrategy:
    # a antiga RandomStrategy (docs/examples/spec-random.json)
    return _spec(
        entry={"conditions": [{"type": "random_chance", "pct": buy}]},
        exit={
            "stop": {"type": "stop_loss", "pct": 50},
            "conditions": [{"type": "random_chance", "pct": sell}],
        },
    )


def _spec(**overrides) -> SpecStrategy:
    return SpecStrategy(StrategySpec.model_validate(make_spec(**overrides)))


def _ticks(prices, step_seconds=60):
    return [
        Tick(START + timedelta(seconds=i * step_seconds), Decimal(str(p)))
        for i, p in enumerate(prices)
    ]


class BuyThenSell(StubStrategy):
    """Compra no primeiro tick e vende no tick `sell_at`."""

    def __init__(self, sell_at):
        super().__init__()
        self.sell_at = sell_at
        self.tick = 0

    def on_market_refresh(
        self, price, balance, current_position, quote_usd: Decimal | None = Decimal(1)
    ):
        self.tick += 1
        if not current_position and self.tick == 1:
            return OrderSignal(OrderSide.BUY, balance / price)
        if current_position and self.tick == self.sell_at:
            return OrderSignal(OrderSide.SELL, current_position.entry_order.quantity)
        return None


async def _run(strategy, prices, **kwargs):
    # contas exatas: sem slippage, salvo quando o teste pede
    kwargs.setdefault("slippage_bps", Decimal("0"))
    return await Backtester(
        strategy, "SOL-USDC", _ticks(prices), Decimal("100"), **kwargs
    ).run()


class TestBacktester:
    async def test_times_are_utc_like_the_ledger(self):
        # soak F13: ticks de candles vêm em hora local sem fuso
        result = await _run(BuyThenSell(sell_at=3), [100, 105, 110])

        times = [result.start, result.end, *(t.timestamp for t in result.trades)]
        assert all(ts.utcoffset() == timedelta(0) for ts in times)
        assert result.start == START.astimezone(UTC)  # o mesmo instante

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

    async def test_the_network_fee_is_paid_on_each_leg(self):
        fee = Decimal("0.5")
        result = await _run(
            BuyThenSell(3), [100, 105, 110], fee_bps=Decimal("0"), network_fee_usd=fee
        )
        # A14: a taxa fica fora do fill, como ao vivo: compra 1 SOL a 100 e
        # vende a 110; as duas taxas saem do PnL e do patrimônio
        assert result.trades[0].price == Decimal(100)
        assert result.trades[0].quantity == ONE
        assert abs(result.final_equity - (110 - 2 * fee)) < LAMPORT_ROUNDING
        realized = result.trades[1].realized_pnl
        assert realized is not None
        assert abs(realized - (10 - 2 * fee)) < LAMPORT_ROUNDING
        assert abs(result.round_trip_costs.cost_usd - 2 * fee) < LAMPORT_ROUNDING

    async def test_stops_are_measured_from_a_fill_without_the_network_fee(self):
        # soak F9: com a taxa no fill, a entrada ficava 0.5% acima do tick e
        # um stop de 1% disparava num recuo de 0.5%
        strategy = _spec(
            entry={"conditions": [{"type": "random_chance", "pct": 100}]},
            exit={"stop": {"type": "stop_loss", "pct": 1}, "conditions": []},
        )
        result = await _run(
            strategy, [100, 99.4, 99.4], fee_bps=Decimal(0), network_fee_usd=Decimal(1)
        )
        assert [t.side for t in result.trades] == [OrderSide.BUY]

    async def test_negative_costs_are_refused(self):
        with pytest.raises(ValueError, match="negativos"):
            await _run(BuyThenSell(3), [100], network_fee_usd=Decimal("-1"))

    async def test_warmup_candles_seed_the_strategy_first(self):
        class Recorder(BuyThenSell):
            seeded = None

            def setup(self, ticker_history):
                self.seeded = (len(ticker_history), self.tick)

        strategy = Recorder(sell_at=99)
        candles = [object(), object()]
        await _run(strategy, [100, 101], warmup=candles)
        assert strategy.seeded == (2, 0)  # antes do primeiro tick

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
            return _random_spec(buy=10, sell=10)

        first = await _run(strategy(), prices, seed=42)
        second = await _run(strategy(), prices, seed=42)
        other_seed = await _run(strategy(), prices, seed=7)

        assert first.trades and first == second
        assert first.trades != other_seed.trades

    async def test_time_based_strategies_use_tick_clock(self):
        # WMA em barras de 1 minuto: com ticks de 60s cada tick é uma barra.
        # Se usasse o relógio real, os ticks (reproduzidos em ms) cairiam todos
        # na mesma barra e a estratégia nunca aqueceria.
        prices = [100 - i for i in range(10)] + [90 + i for i in range(10)]
        wma = {"type": "fast_ma_below_slow", "ma": "wma", "fast": 2, "slow": 4}
        strategy = _spec(
            entry={"conditions": [wma]},
            exit={"stop": {"type": "trailing_stop", "pct": 50}},
        )
        result = await _run(strategy, prices)
        assert result.trades
        assert result.trades[0].side == OrderSide.BUY

    async def test_rejected_signals_are_counted(self):
        class SellWithoutPosition(StubStrategy):
            def on_market_refresh(
                self,
                price,
                balance,
                current_position,
                quote_usd: Decimal | None = Decimal(1),
            ):
                return OrderSignal(OrderSide.SELL, Decimal("1"))

        result = await _run(SellWithoutPosition(), [100, 101])
        assert result.rejected_signals == 2
        assert result.trades == []

    def test_a_non_stable_quote_needs_its_usd_series(self):
        with pytest.raises(ValueError, match="USD de SOL"):
            Backtester(BuyThenSell(2), "JUP-SOL", _ticks([1]), Decimal("1"))

    def test_requires_ticks(self):
        with pytest.raises(ValueError, match="tick"):
            Backtester(BuyThenSell(2), "SOL-USDC", [], Decimal("1"))


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


class TestStrategyDeterminism:
    def test_random_strategy_seed(self):
        def signals(seed):
            strategy = _random_spec(buy=50, sell=50)
            strategy.seed(seed)
            return [
                strategy.on_market_refresh(Decimal("1"), Decimal("1"), None, ONE)
                for _ in range(50)
            ]

        assert signals(1) == signals(1)
        assert signals(1) != signals(2)


class Churn(StubStrategy):
    """Compra sem posição, vende com posição: um tick cada."""

    def on_market_refresh(
        self, price, balance, current_position, quote_usd: Decimal | None = Decimal(1)
    ):
        if current_position:
            return OrderSignal(OrderSide.SELL, current_position.entry_order.quantity)
        return OrderSignal(OrderSide.BUY, Decimal("10") / price)


async def test_a_retired_bucket_stops_the_replay():
    prices = [100, 90, 90, 80, 80, 70, 70, 60, 60, 50]

    result = await _run(
        Churn(),
        prices,
        fee_bps=Decimal("0"),
        budget_usd=Decimal("50"),
        max_loss_usd=Decimal("1"),
    )

    # perde 1 USD na primeira volta: o bucket encerra e o replay para de operar
    assert len(result.trades) == 2
    assert result.rejected_signals == 0
