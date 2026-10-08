from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trader.shared import indicators as ind
from trader.shared.models import Interval, TickerData


def D(*values):
    return [Decimal(str(v)) for v in values]


class TestMovingAverages:
    def test_sma(self):
        assert ind.sma(D(1, 2, 3, 4), 2) == Decimal("3.5")
        assert ind.sma(D(1), 2) is None

    def test_wma_weights_recent_prices_more(self):
        assert ind.wma(D(1, 2, 3), 3) == Decimal(14) / Decimal(6)
        assert ind.wma(D(1, 2), 3) is None

    def test_ema_seeded_by_sma(self):
        # semente (1+2+3)/3 = 2; alpha 0.5 -> 3 -> 4
        assert ind.ema(D(1, 2, 3, 4, 5), 3) == Decimal(4)
        assert ind.ema(D(1, 2), 3) is None


class TestRegistry:
    def test_bars_to_have_a_value_and_to_converge(self):
        # o que as condições tipadas e o `expr` pedem de aquecimento (A17)
        assert [ind.lookback(n, 14) for n in ("sma", "rsi", "volatility")] == [
            14,
            15,
            15,
        ]
        assert [ind.history(n, 14) for n in ("sma", "ema", "rsi")] == [14, 70, 141]

    def test_every_indicator_has_a_value_after_its_lookback(self):
        values = D(*range(1, 40))
        for name, fn in ind.INDICATORS.items():
            assert fn(values[: ind.lookback(name, 10)], 10) is not None, name
            assert fn(values[: ind.lookback(name, 10) - 1], 10) is None, name


class TestRsi:
    def test_hand_computed_wilder_value(self):
        # ganhos/perdas alternados: 0.5/0.5 -> 0.75/0.25 -> 0.375/0.625
        assert ind.rsi(D(1, 2, 1, 2, 1), 2) == Decimal("37.5")

    @pytest.mark.parametrize(
        ("values", "expected"),
        [((1, 2, 3, 4), 100), ((4, 3, 2, 1), 0), ((5, 5, 5, 5), 50)],
    )
    def test_extremes(self, values, expected):
        assert ind.rsi(D(*values), 3) == Decimal(expected)

    def test_needs_period_plus_one_values(self):
        assert ind.rsi(D(1, 2, 3), 3) is None


class TestChangeAndVolatility:
    def test_volatility_is_stdev_of_returns(self):
        # retornos +10% e -10%: média 0, desvio 10
        assert ind.volatility(D(100, 110, 99), 2) == Decimal(10)
        assert ind.volatility(D(100, 110), 2) is None

    def test_rolling_high_and_low(self):
        values = D(3, 9, 1, 5)
        assert ind.rolling_high(values, 3) == Decimal(9)
        assert ind.rolling_low(values, 2) == Decimal(1)
        assert ind.rolling_high(values, 5) is None


class TestTimestamps:
    def test_to_utc_accepts_naive_local_and_aware(self):
        aware = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
        naive_local = aware.astimezone().replace(tzinfo=None)
        assert ind.to_utc(naive_local) == aware
        assert ind.to_utc(aware) == aware


T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


class TestBarSeries:
    def test_forming_bar_takes_the_latest_price(self):
        bars = ind.BarSeries(Interval.MINUTE_1, maxlen=10)
        assert bars.update(T0, Decimal(1))
        assert not bars.update(T0 + timedelta(seconds=30), Decimal(2))
        assert bars.update(T0 + timedelta(seconds=60), Decimal(3))
        assert bars.closes == D(2, 3)

    def test_out_of_order_tick_updates_the_current_bar(self):
        bars = ind.BarSeries(Interval.MINUTE_1, maxlen=10)
        bars.update(T0 + timedelta(minutes=5), Decimal(1))
        assert not bars.update(T0, Decimal(7))
        assert bars.closes == D(7)

    def test_short_gaps_repeat_the_last_close(self):
        bars = ind.BarSeries(Interval.MINUTE_1, maxlen=20)
        bars.update(T0, Decimal(1))
        bars.update(T0 + timedelta(minutes=ind.MAX_GAP_BARS + 1), Decimal(2))
        assert bars.closes == D(*[1] * (ind.MAX_GAP_BARS + 1), 2)

    def test_a_long_gap_restarts_the_series(self):
        # T4: um buraco longo virava barras planas (RSI 0/100, volatilidade ~0)
        bars = ind.BarSeries(Interval.MINUTE_1, maxlen=20)
        for i in range(10):
            bars.update(T0 + timedelta(minutes=i), Decimal(i))
        bars.update(T0 + timedelta(minutes=9 + ind.MAX_GAP_BARS + 2), Decimal(5))
        assert bars.closes == D(5)

    def test_maxlen_keeps_the_most_recent_bars(self):
        bars = ind.BarSeries(Interval.SECOND_15, maxlen=2)
        for i in range(4):
            bars.update(T0 + timedelta(seconds=15 * i), Decimal(i))
        assert bars.closes == D(2, 3)
        assert len(bars) == 2

    def test_seed_from_unsorted_candles(self):
        bars = ind.BarSeries(Interval.MINUTE_1, maxlen=10)
        bars.seed([_candle(2, 30), _candle(0, 10), _candle(1, 20)])
        assert bars.closes == D(10, 20, 30)


class TestSeedAcrossQuietBars:
    """A4: um candle só existe numa barra com negócio."""

    def test_seed_fills_a_long_gap_with_the_previous_close(self):
        bars = ind.BarSeries(Interval.MINUTE_1, maxlen=20)
        bars.seed([_candle(0, 1), _candle(ind.MAX_GAP_BARS + 3, 2)])
        assert bars.closes == D(*[1] * (ind.MAX_GAP_BARS + 3), 2)

    def test_a_gap_longer_than_the_window_leaves_a_flat_window(self):
        bars = ind.BarSeries(Interval.MINUTE_1, maxlen=4)
        bars.seed([_candle(0, 7), _candle(10_000, 8)])
        assert bars.closes == D(7, 7, 7, 8)

    def test_seed_until_now_fills_from_the_last_trade(self):
        # o último candle é o último negócio, talvez de horas atrás
        bars = ind.BarSeries(Interval.MINUTE_1, maxlen=20)
        now = T0 + timedelta(minutes=60)
        bars.seed([_candle(0, 1), _candle(1, 2)], until=now)
        assert bars.closes == D(*[2] * 20)  # a barra de agora começa em 2
        assert not bars.update(now, Decimal(3))
        assert bars.closes == D(*[2] * 19, 3)

    def test_a_later_live_gap_still_restarts_the_series(self):
        bars = ind.BarSeries(Interval.MINUTE_1, maxlen=20)
        bars.seed([_candle(0, 1)], until=T0 + timedelta(minutes=30))
        bars.update(T0 + timedelta(minutes=30), Decimal(2))
        bars.update(T0 + timedelta(minutes=30 + ind.MAX_GAP_BARS + 2), Decimal(3))
        assert bars.closes == D(3)

    def test_without_until_the_gap_to_the_first_tick_is_a_live_gap(self):
        bars = ind.BarSeries(Interval.MINUTE_1, maxlen=20)
        bars.seed([_candle(0, 1)])
        bars.update(T0 + timedelta(minutes=ind.MAX_GAP_BARS + 2), Decimal(2))
        assert bars.closes == D(2)


def _candle(minutes, price):
    return TickerData(
        timestamp=T0 + timedelta(minutes=minutes),
        high=Decimal(0),
        last=Decimal(price),
        low=Decimal(0),
        open=Decimal(0),
    )
