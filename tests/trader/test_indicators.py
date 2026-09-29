from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trader import indicators as ind
from trader.models import Interval, TickerData
from trader.trading_strategy import WeightedMovingAverageStrategy


def D(*values):
    return [Decimal(str(v)) for v in values]


class TestMovingAverages:
    def test_sma(self):
        assert ind.sma(D(1, 2, 3, 4), 2) == Decimal("3.5")
        assert ind.sma(D(1), 2) is None

    def test_wma_weights_recent_prices_more(self):
        assert ind.wma(D(1, 2, 3), 3) == Decimal(14) / Decimal(6)
        assert ind.wma(D(1, 2), 3) is None

    def test_wma_matches_the_legacy_strategy(self):
        prices = D(10, 11, 12.5, 11.8, 13, 12.2, 14, 13.7)
        legacy = WeightedMovingAverageStrategy(short_window=5, long_window=5)
        for window in (2, 5, 8):
            assert ind.wma(prices, window) == legacy.weighted_moving_average(
                prices, window
            )

    def test_ema_seeded_by_sma(self):
        # semente (1+2+3)/3 = 2; alpha 0.5 -> 3 -> 4
        assert ind.ema(D(1, 2, 3, 4, 5), 3) == Decimal(4)
        assert ind.ema(D(1, 2), 3) is None

    def test_moving_average_dispatch(self):
        values = D(1, 2, 3, 4)
        for kind in ("sma", "ema", "wma"):
            assert ind.moving_average(kind, values, 3) == getattr(ind, kind)(values, 3)


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
    def test_pct_change(self):
        assert ind.pct_change(D(100, 105, 110), 2) == Decimal(10)
        assert ind.pct_change(D(100), 1) is None
        assert ind.pct_change(D(0, 1), 1) is None

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

    def test_maxlen_keeps_the_most_recent_bars(self):
        bars = ind.BarSeries(Interval.SECOND_15, maxlen=2)
        for i in range(4):
            bars.update(T0 + timedelta(seconds=15 * i), Decimal(i))
        assert bars.closes == D(2, 3)
        assert len(bars) == 2

    def test_seed_from_unsorted_candles(self):
        def candle(minutes, price):
            return TickerData(
                buy=Decimal(0),
                timestamp=T0 + timedelta(minutes=minutes),
                high=Decimal(0),
                last=Decimal(price),
                low=Decimal(0),
                open=Decimal(0),
                pair="x",
                sell=Decimal(0),
                vol=Decimal(0),
            )

        bars = ind.BarSeries(Interval.MINUTE_1, maxlen=10)
        bars.seed([candle(2, 30), candle(0, 10), candle(1, 20)])
        assert bars.closes == D(10, 20, 30)
