"""A37: os indicadores em duas partes (barras fechadas + último preço) dão o
mesmo valor que a função inteira, bit a bit."""

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trader.shared import indicators as ind
from trader.shared.models import Interval
from trader.strategy.spec.conditions import IndicatorBank

T0 = datetime(2025, 10, 10, 12, 0, tzinfo=UTC)


def _series(n: int, seed: int) -> list[Decimal]:
    rng = random.Random(seed)
    price = Decimal(100)
    out = []
    for _ in range(n):
        price *= Decimal(1) + Decimal(rng.randint(-300, 300)) / 10_000
        out.append(price)
    return out


@pytest.mark.parametrize("name", sorted(ind.PARTIAL))
def test_split_equals_full_for_every_window(name):
    prefix, finish = ind.PARTIAL[name]
    split = 0
    for seed in range(3):
        values = _series(30, seed)
        for n in range(1, len(values) + 1):
            closed, last = values[: n - 1], values[n - 1]
            for window in range(0, 16):
                state = prefix(closed, window)
                if state is None:
                    continue  # quem chama usa a função inteira
                full = ind.INDICATORS[name](values[:n], window)
                assert finish(state, last, window) == full, (name, n, window)
                split += 1
    assert split > 500  # a separação vale na maior parte dos casos


def test_the_bank_matches_a_full_computation_tick_by_tick():
    bank = IndicatorBank(Interval.MINUTE_1, 50)
    rng = random.Random(7)
    price = Decimal(100)
    names = [("sma", 5), ("wma", 9), ("ema", 12), ("rsi", 6), ("volatility", 5)]
    for i in range(300):
        # 4 ticks por barra, uma barra sem tick de vez em quando (preenchida)
        ts = T0 + timedelta(seconds=15 * i + (60 if i % 97 == 0 else 0))
        price *= Decimal(1) + Decimal(rng.randint(-200, 200)) / 10_000
        bank.update(ts, price)
        closes = bank.bars.closes
        for name, window in names:
            assert bank.get(name, window) == ind.INDICATORS[name](closes, window)


def test_a_seed_and_a_reset_start_the_states_over():
    bank = IndicatorBank(Interval.MINUTE_1, 50)
    for i, p in enumerate(_series(30, 1)):
        bank.update(T0 + timedelta(minutes=i), p)
    bank.get("ema", 5)
    # um buraco maior que MAX_GAP_BARS zera a série
    later = T0 + timedelta(hours=5)
    for i, p in enumerate(_series(10, 2)):
        bank.update(later + timedelta(minutes=i), p)
        assert bank.get("ema", 5) == ind.ema(bank.bars.closes, 5)
