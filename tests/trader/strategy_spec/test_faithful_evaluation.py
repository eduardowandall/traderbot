"""R7: indicadores convergidos e backtests sem otimismo embutido."""

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from factories import make_spec

from trader import indicators as ind
from trader.backtest import Backtester, Tick, ticks_from_candles
from trader.backtest import spec as strategies
from trader.backtest.ticks import PATH_STEPS
from trader.models import Interval, OrderSide, TickerData
from trader.strategy_spec.models import StrategySpec
from trader.strategy_spec.strategy import SpecStrategy

ONE = Decimal(1)

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
MINUTE = Interval.MINUTE_1


def _candle(i, o, h, low, c):
    return TickerData(
        timestamp=T0 + timedelta(minutes=i),
        high=Decimal(h),
        last=Decimal(c),
        low=Decimal(low),
        open=Decimal(o),
    )


def _walk(n, seed=7):
    rng = random.Random(seed)
    price, closes = Decimal(100), []
    for _ in range(n):
        price *= Decimal(1) + Decimal(rng.randint(-100, 100)) / 10000
        closes.append(price)
    return closes


class TestCandlePaths:
    def test_each_closed_bar_is_open_low_high_close_inside_the_bar(self):
        candles = [_candle(0, 100, 105, 95, 102), _candle(1, 102, 103, 101, 101)]

        ticks = ticks_from_candles(
            candles, MINUTE, now=T0 + timedelta(minutes=1, seconds=30)
        )

        # o segundo candle ainda está em formação: fica de fora
        prices = [t.price for t in ticks]
        assert len(prices) == 1 + 3 * PATH_STEPS
        # passa pelos quatro pontos, na ordem abertura -> mín -> máx -> fech
        keys = [prices.index(Decimal(p)) for p in (100, 95, 105, 102)]
        assert keys == sorted(keys) and prices[-1] == 102
        assert ticks[0].timestamp == T0
        assert T0 < ticks[-1].timestamp < T0 + timedelta(minutes=1)

    def test_a_falling_bar_visits_the_high_first(self):
        # T5: sempre mín -> máx deixava uma compra na queda + take profit na
        # mesma barra de baixa sair de graça
        ticks = ticks_from_candles(
            [_candle(0, 100, 105, 95, 97)], MINUTE, now=T0 + timedelta(minutes=2)
        )
        prices = [t.price for t in ticks]
        keys = [prices.index(Decimal(p)) for p in (100, 105, 95, 97)]
        assert keys == sorted(keys)


class TestIntrabarStops:
    async def test_a_stop_fires_on_the_bar_low_even_if_the_close_recovers(self):
        spec = StrategySpec.model_validate(
            make_spec(
                entry={
                    "mode": "all",
                    "conditions": [{"type": "price_below", "value": 101}],
                },
                exit={
                    "stop": {"type": "stop_loss", "pct": 5},
                    "mode": "any",
                    "conditions": [{"type": "take_profit", "pct": 50}],
                },
            )
        )
        candles = [
            _candle(0, 100, 100, 100, 100),  # compra a 100
            _candle(1, 100, 101, 94, 99),  # mínima 94 (stop de 5%), fecha 99
            _candle(2, 99, 99, 99, 99),
        ]
        ticks = ticks_from_candles(candles, MINUTE, now=T0 + timedelta(minutes=10))

        result = await Backtester(
            SpecStrategy(spec),
            "SOL-USDC",
            ticks,
            Decimal(100),
            fee_bps=Decimal(0),
            slippage_bps=Decimal(0),
        ).run()

        # só com fechamentos, o stop nunca dispararia (a barra fecha a 99)
        buy, stop = result.trades[:2]
        assert (buy.side, stop.side) == (OrderSide.BUY, OrderSide.SELL)
        # o stop de 5% dispara perto de 95, no caminho até a mínima de 94
        assert Decimal(94) < stop.price <= Decimal(95)


class TestConvergence:
    def test_a_warmed_spec_agrees_with_the_long_run_indicator(self):
        closes = _walk(600)
        spec = StrategySpec.model_validate(
            make_spec(
                entry={
                    "mode": "all",
                    "conditions": [
                        {"type": "rsi_below", "period": 14, "value": 50},
                        {"type": "price_above_ma", "ma": "ema", "window": 50},
                    ],
                }
            )
        )
        strategy = SpecStrategy(spec)
        _, warmup = strategy.warmup()
        assert warmup == 250  # 5x a EMA50
        strategy.bank.seed(
            [_candle(i, c, c, c, c) for i, c in enumerate(closes)][-warmup:]
        )

        rsi_spec = strategy.bank.get("rsi", 14)
        ema_spec = strategy.bank.get("ema", 50)

        rsi_full, ema_full = ind.rsi(closes, 14), ind.ema(closes, 50)
        assert None not in (rsi_spec, ema_spec, rsi_full, ema_full)
        assert rsi_spec and ema_spec and rsi_full and ema_full
        assert abs(rsi_spec - rsi_full) < Decimal("0.01")
        assert abs(ema_spec / ema_full - 1) < Decimal("0.0001")


class TestBacktestGuards:
    async def test_too_few_bars_is_an_error_not_an_empty_success(self):
        spec = StrategySpec.model_validate(
            make_spec(
                entry={
                    "mode": "all",
                    "conditions": [{"type": "rsi_below", "period": 14, "value": 30}],
                }
            )
        )
        ticks = [Tick(T0 + timedelta(minutes=i), Decimal(100)) for i in range(20)]

        with pytest.raises(ValueError, match="aquecimento de 141 barras"):
            await strategies.backtest_spec(spec, ticks, Decimal(30), "0")

    async def test_the_result_reports_bars_and_costs(self):
        spec = StrategySpec.model_validate(make_spec())
        ticks = [Tick(T0 + timedelta(minutes=i), Decimal(100)) for i in range(60)]

        result = await strategies.backtest_spec(
            spec, ticks, Decimal(30), "0", Decimal(5)
        )

        assert result["bars"] == 60
        assert result["warmup_bars"] == spec.history()
        assert result["slippage_bps"] == Decimal(5)


class TestStageS1:
    async def test_a_dip_and_take_profit_in_one_bar_fill_near_their_levels(self):
        spec = StrategySpec.model_validate(
            make_spec(
                entry={
                    "mode": "all",
                    "conditions": [{"type": "price_below", "value": 96}],
                },
                exit={
                    "stop": {"type": "stop_loss", "pct": 20},
                    "mode": "any",
                    "conditions": [{"type": "take_profit", "pct": 3}],
                },
            )
        )
        candles = [_candle(0, 100, 101, 90, 100)]  # o probe do revisor
        ticks = ticks_from_candles(candles, MINUTE, now=T0 + timedelta(minutes=5))

        result = await Backtester(
            SpecStrategy(spec),
            "SOL-USDC",
            ticks,
            Decimal(100),
            fee_bps=Decimal(0),
            slippage_bps=Decimal(0),
        ).run()

        buy, sell = result.trades[:2]
        assert Decimal(94) < buy.price <= Decimal(96)  # não a mínima de 90
        assert sell.price < Decimal(101)  # não a máxima
        assert result.return_pct < Decimal(8)  # antes: +12%

    def test_the_longest_warm_up_fits_the_candle_limit(self):
        spec = StrategySpec.model_validate(
            make_spec(
                entry={
                    "mode": "all",
                    "conditions": [
                        {"type": "price_above_ma", "ma": "ema", "window": 500}
                    ],
                }
            )
        )
        assert spec.history() + strategies.MIN_EVAL_BARS <= 999

    async def test_negative_costs_are_rejected(self):
        spec = StrategySpec.model_validate(make_spec())
        ticks = [Tick(T0 + timedelta(minutes=i), Decimal(100)) for i in range(80)]
        with pytest.raises(ValueError, match="negativos"):
            await strategies.backtest_spec(spec, ticks, Decimal(-5), "0")


class TestFeedGaps:
    def test_a_long_gap_cools_the_strategy_down(self):
        # T4: depois de um buraco no feed as entradas esperam aquecer de novo
        spec = make_spec(
            entry={"conditions": [{"type": "rsi_below", "period": 2, "value": 99}]}
        )
        strategy = SpecStrategy(StrategySpec.model_validate(spec))
        now = [T0]
        strategy.set_clock(lambda: now[0])
        history = strategy.spec.history()

        def tick(minute, price):
            now[0] = T0 + timedelta(minutes=minute)
            return strategy.on_market_refresh(Decimal(price), Decimal(100), None, ONE)

        signals = [tick(i, 90 + i % 3) for i in range(history + 1)]
        assert signals[-1] and signals[-1].side == OrderSide.BUY  # aquecida
        gap = history + ind.MAX_GAP_BARS + 2
        assert tick(gap, 91) is None  # esfriou: a série recomeçou
