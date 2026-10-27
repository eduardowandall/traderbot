"""Cruzamentos (A22): o indicador `back` barras atrás, e os blocos tipados."""

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from factories import make_spec
from pydantic import ValidationError

from trader.shared import indicators as ind
from trader.shared.models.public_data import Interval, TickerData
from trader.strategy.spec.conditions import IndicatorBank, TickContext, holds
from trader.strategy.spec.models import MaCrossesAbove, MaCrossesBelow, StrategySpec

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _bank(prices: list[int], ticks_per_bar: int = 1) -> IndicatorBank:
    bank = IndicatorBank(Interval.MINUTE_1, 200)
    for minute, price in enumerate(prices):
        for second in range(ticks_per_bar):
            bank.update(T0 + timedelta(minutes=minute, seconds=second), Decimal(price))
    return bank


class TestBarsBack:
    def test_back_is_the_value_at_an_earlier_closed_bar(self):
        prices = [10, 12, 11, 15, 14, 18, 17]
        bank = _bank(prices)
        for back in range(4):
            earlier = _bank(prices[: len(prices) - back])
            assert bank.get("ema", 3, back) == earlier.get("ema", 3)
        assert bank.get("ema", 3, 0) == bank.get("ema", 3)

    def test_ticks_inside_the_forming_bar_do_not_move_the_closed_ones(self):
        bank = _bank([10, 12, 11])
        closed = bank.get("sma", 2, 1)
        bank.update(T0 + timedelta(minutes=2, seconds=30), Decimal(99))
        assert bank.get("sma", 2, 1) == closed == Decimal("11.0")
        assert bank.get("sma", 2) == Decimal("55.5")

    def test_too_few_bars_back_is_unknown(self):
        bank = _bank([10, 12, 11, 13])
        assert bank.get("sma", 3, 1) is not None
        assert bank.get("sma", 3, 2) is None
        assert bank.get("sma", 3, 50) is None


def _ticks(condition, prices: list[int]) -> list[int]:
    """Os minutos em que `condition` vale, um tick por minuto."""
    bank = IndicatorBank(Interval.MINUTE_1, 200)
    fired = []
    for minute, price in enumerate(prices):
        bank.update(T0 + timedelta(minutes=minute), Decimal(price))
        if holds(condition, TickContext(price=Decimal(price), now=T0, bank=bank)):
            fired.append(minute)
    return fired


# cai 10 barras, sobe 10, cai 10: a rápida cruza para cima e depois para baixo
V_THEN_DOWN = [*range(120, 100, -2), *range(100, 140, 4), *range(140, 100, -4)]


class TestTypedCrosses:
    def test_above_fires_only_on_the_bar_of_the_cross(self):
        above = MaCrossesAbove(type="ma_crosses_above", fast=2, slow=5)
        (minute,) = _ticks(above, V_THEN_DOWN)
        assert 10 < minute < 20
        assert above.label() == "cross:sma2>sma5"

    def test_below_fires_only_on_the_bar_of_the_cross(self):
        below = MaCrossesBelow(type="ma_crosses_below", ma="ema", fast=2, slow=5)
        (minute,) = _ticks(below, V_THEN_DOWN)
        assert minute >= 20
        assert below.label() == "cross:ema2<ema5"

    def test_no_cross_while_warming_up(self):
        above = MaCrossesAbove(type="ma_crosses_above", fast=2, slow=5)
        assert _ticks(above, list(range(100, 106))) == []  # 6 barras: falta 1

    def test_fast_must_be_shorter_than_slow(self):
        with pytest.raises(ValidationError, match="fast precisa ser menor"):
            MaCrossesAbove(type="ma_crosses_above", fast=21, slow=9)

    def test_warmup_counts_the_bar_back(self):
        spec = StrategySpec.model_validate(
            make_spec(
                entry={
                    "conditions": [
                        {"type": "ma_crosses_above", "ma": "ema", "fast": 9, "slow": 21}
                    ]
                }
            )
        )
        assert (spec.lookback(), spec.history()) == (22, 106)


class TestClosedBarValues:
    def test_a_closed_bar_keeps_the_value_it_ended_with(self):
        # série cheia (6 barras): recalcular a EMA sobre a janela que andou
        # começa uma barra depois e dá outro valor; `back` lê o do fechamento
        bank = IndicatorBank(Interval.MINUTE_1, 6)
        rng, price, ended, drifted = random.Random(3), Decimal(100), None, False
        for minute in range(40):
            price *= 1 + Decimal(rng.randint(-300, 300)) / 10000
            bank.update(T0 + timedelta(minutes=minute), price)
            if ended is not None:
                assert bank.get("ema", 3, 1) == ended
                drifted |= ind.ema(bank.bars.closes[:-1], 3) != ended
            ended = bank.get("ema", 3)
        assert drifted  # o teste pega o recálculo

    def test_a_closed_bar_is_not_recomputed(self, monkeypatch):
        bank = _bank([10, 12, 11])
        bank.get("sma", 2)
        calls = []
        sma = ind.INDICATORS["sma"]
        monkeypatch.setitem(
            ind.INDICATORS, "sma", lambda v, w: calls.append(w) or sma(v, w)
        )
        bank.update(T0 + timedelta(minutes=3), Decimal(50))
        for second in (10, 20):
            assert bank.get("sma", 2, 1) == Decimal("11.5")
            bank.update(T0 + timedelta(minutes=3, seconds=second), Decimal(60))
        assert calls == []

    def test_a_seed_forgets_only_the_bars_that_left_the_series(self):
        bank = IndicatorBank(Interval.MINUTE_1, 3)
        for minute, price in enumerate([10, 12, 11, 13]):
            bank.update(T0 + timedelta(minutes=minute), Decimal(price))
            bank.get("sma", 2)  # barras 1, 2, 3: 11, 11.5, 12
        bank.seed([_candle(4, 20)])  # fica [11, 13, 20]: a barra 1 saiu
        assert bank.get("sma", 2, 1) == Decimal(12)
        assert bank.get("sma", 2, 2) == Decimal("11.5")  # a que a barra 2 fechou
        assert bank.get("sma", 2, 3) is None


def _candle(minute: int, close: int) -> TickerData:
    c = Decimal(close)
    return TickerData(
        timestamp=T0 + timedelta(minutes=minute), high=c, last=c, low=c, open=c
    )
