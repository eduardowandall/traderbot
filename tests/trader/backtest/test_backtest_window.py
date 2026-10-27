"""A34: uma janela do passado, com aquecimento antes e buracos recusados."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from factories import FakeRange, candle, make_spec

from trader.backtest.spec import backtest_spec
from trader.backtest.window import MAX_HOLE, coverage, fetch_window, holes
from trader.shared.indicators import to_utc
from trader.shared.models import SOLANA_MINTS, Interval, TickerData
from trader.strategy.spec.models import StrategySpec

MINUTE = Interval.MINUTE_1
T0 = datetime(2025, 10, 10, 12, 0, tzinfo=UTC)
SOL = SOLANA_MINTS.get_by_symbol("SOL").mint
JUP = SOLANA_MINTS.get_by_symbol("JUP").mint
# rsi(2): 21 barras de aquecimento
RSI = {"mode": "all", "conditions": [{"type": "rsi_below", "period": 2, "value": 30}]}


def _minutes(*offsets: int) -> list[TickerData]:
    return [candle(T0 + timedelta(minutes=m), Decimal(1)) for m in offsets]


def _spec(**overrides) -> StrategySpec:
    return StrategySpec.model_validate(make_spec(entry=RSI, **overrides))


class TestHoles:
    def test_a_full_window_has_none(self):
        end = T0 + timedelta(minutes=10)
        assert holes(_minutes(*range(10)), MINUTE, T0, end) == []

    def test_short_gaps_are_quiet_bars(self):
        end = T0 + timedelta(minutes=120)
        present = [m for m in range(120) if not 10 <= m < 70]  # 60 min: = MAX_HOLE
        assert holes(_minutes(*present), MINUTE, T0, end) == []

    def test_a_long_gap_is_listed(self):
        end = T0 + timedelta(minutes=120)
        present = [m for m in range(120) if not 10 <= m < 71]

        assert holes(_minutes(*present), MINUTE, T0, end) == [
            (T0 + timedelta(minutes=10), T0 + timedelta(minutes=71))
        ]

    def test_the_edges_count(self):
        end = T0 + timedelta(hours=4)
        middle = _minutes(*range(90, 150))

        assert holes(middle, MINUTE, T0, end) == [
            (T0, T0 + timedelta(minutes=90)),
            (T0 + timedelta(minutes=150), end),
        ]
        assert holes([], MINUTE, T0, end) == [(T0, end)]

    def test_coverage_counts_the_bars_with_a_candle(self):
        cover = coverage(_minutes(0, 1, 3), MINUTE, T0, T0 + timedelta(minutes=4))
        assert (cover.bars, cover.expected, cover.pct) == (3, 4, 75.0)


class TestFetchWindow:
    async def test_warms_up_before_the_window_and_ticks_inside_it(self):
        spec = _spec()
        source = FakeRange({SOL: Decimal(150)})
        end = T0 + timedelta(minutes=30)

        window = await fetch_window(source, spec, T0, end, now=T0 + timedelta(days=1))

        warm = spec.history()
        assert source.asked == [(SOL, T0 - timedelta(minutes=warm), end)]
        assert len(window.warmup) == warm
        assert all(to_utc(c.timestamp) < T0 for c in window.warmup)
        assert to_utc(window.ticks[0].timestamp) == T0
        assert to_utc(window.ticks[-1].timestamp) < end
        assert window.coverage.as_dict() == {"bars": 30, "expected": 30, "pct": 100.0}

    async def test_a_non_stable_pair_reads_both_series(self):
        spec = _spec(symbol="JUP-SOL")
        source = FakeRange({JUP: Decimal(1), SOL: Decimal(200)})
        end = T0 + timedelta(minutes=30)

        window = await fetch_window(source, spec, T0, end, now=end)

        assert {mint for mint, *_ in source.asked} == {JUP, SOL}
        assert window.ticks[0].price == Decimal("0.005")
        assert window.ticks[0].quote_usd == Decimal(200)
        assert window.warmup[0].last == Decimal("0.005")

    async def test_stops_at_the_last_closed_bar(self):
        now = T0 + timedelta(minutes=30, seconds=20)
        window = await fetch_window(
            FakeRange({SOL: Decimal(1)}), _spec(), T0, T0 + timedelta(days=1), now=now
        )

        assert window.end == T0 + timedelta(minutes=30)
        assert window.coverage.expected == 30

    async def test_refuses_a_hole_in_the_warmup(self):
        # rsi(14): 141 barras de 1 min antes da janela, mais que MAX_HOLE
        entry = {**RSI, "conditions": [{**RSI["conditions"][0], "period": 14}]}
        spec = StrategySpec.model_validate(make_spec(entry=entry))
        assert spec.history() * 60 > MAX_HOLE.total_seconds()
        hole = [T0 - timedelta(minutes=m) for m in range(1, spec.history() + 1)]
        end = T0 + timedelta(hours=3)
        source = FakeRange({SOL: Decimal(1)}, missing=hole)

        with pytest.raises(ValueError, match="a API não tem candles"):
            await fetch_window(source, spec, T0, end, now=end)

    async def test_refuses_an_empty_window(self):
        with pytest.raises(ValueError, match="janela vazia"):
            await fetch_window(FakeRange({SOL: Decimal(1)}), _spec(), T0, T0, now=T0)


async def test_backtest_spec_warms_up_from_the_window():
    spec = _spec()
    end = T0 + timedelta(minutes=25)
    window = await fetch_window(FakeRange({SOL: Decimal(150)}), spec, T0, end, now=end)

    result = await backtest_spec(spec, window.ticks, warmup=window.warmup)

    assert result["bars"] == 25
    with pytest.raises(ValueError, match="aquecimento de 21 barras"):
        await backtest_spec(spec, window.ticks)


async def test_a_short_warmup_counts_only_the_bars_it_covers():
    spec = _spec()
    end = T0 + timedelta(minutes=25)
    window = await fetch_window(FakeRange({SOL: Decimal(150)}), spec, T0, end, now=end)

    # 1 candle de aquecimento, colado na janela: faltam 20 das 21 barras
    with pytest.raises(ValueError, match="aquecimento de 20 barras"):
        await backtest_spec(spec, window.ticks, warmup=window.warmup[-1:])
