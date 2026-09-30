"""Blocos que substituíram as estratégias legadas (plan A11) e os exemplos."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from factories import example_spec, make_spec

from trader.models import SOLANA_MINTS, Order, OrderSide, Position, PositionType
from trader.strategy_spec.models import StrategySpec
from trader.strategy_spec.strategy import SpecStrategy
from trader.strategy_spec.validate import SpecLimits, parse_spec, validate

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
USDC = SOLANA_MINTS.get_by_symbol("USDC").mint
SOL = SOLANA_MINTS.get_by_symbol("SOL").mint
STOP_10 = {"type": "stop_loss", "pct": 10}


def _strategy(**overrides) -> SpecStrategy:
    strategy = SpecStrategy(StrategySpec.model_validate(make_spec(**overrides)))
    ticks = iter(T0 + timedelta(minutes=i) for i in range(10_000))
    strategy.set_clock(lambda: next(ticks))
    return strategy


def _position(price="100") -> Position:
    order = Order("buy-1", USDC, SOL, Decimal(1), Decimal(price), OrderSide.BUY, T0)
    return Position(PositionType.LONG, order, None)


def _signals(strategy, prices, position=None):
    return [
        strategy.on_market_refresh(Decimal(str(p)), None, Decimal(100), position)
        for p in prices
    ]


@pytest.mark.parametrize(
    "name", ["random", "target-value", "wma-composer", "scalp-test"]
)
def test_example_specs_validate_under_the_default_limits(name):
    with open(example_spec(name), encoding="utf-8") as f:
        spec = parse_spec(f.read())
    assert validate(spec, SpecLimits(max_trade_usd=Decimal(25))) == []


class TestRandomChance:
    def _random(self, pct):
        return _strategy(
            entry={"conditions": [{"type": "random_chance", "pct": pct}]},
        )

    def test_the_seed_fixes_the_draws(self):
        def draws(seed):
            strategy = self._random(50)
            strategy.seed(seed)
            return [s is not None for s in _signals(strategy, [50] * 40)]

        assert draws(1) == draws(1)
        assert draws(1) != draws(2)
        assert 0 < sum(draws(1)) < 40

    def test_one_hundred_percent_always_holds(self):
        signal = _signals(self._random(100), [50])[0]
        assert signal and signal.side == OrderSide.BUY
        assert "chance100%" in (signal.rationale or "")

    def test_rejects_out_of_range(self):
        for pct in (0, 101):
            spec = make_spec(
                entry={"conditions": [{"type": "random_chance", "pct": pct}]}
            )
            with pytest.raises(ValueError):
                StrategySpec.model_validate(spec)


class TestTrailingTakeProfit:
    def _strategy(self):
        ttp = {"type": "trailing_take_profit", "pct": 5, "trail_pct": 1}
        return _strategy(exit={"stop": STOP_10, "conditions": [ttp]})

    def test_waits_for_the_target_then_trails_the_peak(self):
        signals = _signals(self._strategy(), [104, 103, 106, 105.5, 104.9], _position())
        # 104 e 103: abaixo do alvo de +5%; 106 arma; 104.9 cai >= 1% do pico
        assert signals[:4] == [None] * 4
        assert signals[4] and signals[4].side == OrderSide.SELL
        assert "trailing_take_profit5%-1%" in (signals[4].rationale or "")

    def test_stays_armed_below_the_target_band(self):
        # a legada parava de acompanhar abaixo de alvo - 1.1% e não vendia
        signals = _signals(self._strategy(), [106, 101], _position())
        assert signals[0] is None
        assert signals[1] and signals[1].side == OrderSide.SELL


def test_rebound_from_low_waits_for_the_price_to_turn():
    rebound = {"type": "rebound_from_low", "window": 3, "pct": 1}
    strategy = _strategy(entry={"conditions": [rebound]})
    signals = _signals(strategy, [100, 95, 95.5, 96])
    # mínima de 95: 95.5 ainda está a menos de 1% dela, 96 já passou
    assert signals[:3] == [None] * 3
    assert signals[3] and signals[3].side == OrderSide.BUY


def test_target_value_example_buys_below_target_after_a_rebound():
    with open(example_spec("target-value"), encoding="utf-8") as f:
        data = json.load(f)
    data["entry"]["conditions"][0]["value"] = 100
    strategy = SpecStrategy(StrategySpec.model_validate(data))
    ticks = iter(T0 + timedelta(minutes=i) for i in range(100))
    strategy.set_clock(lambda: next(ticks))
    signals = _signals(strategy, [101, 99, 98, 98.5])
    assert signals[:3] == [None] * 3  # acima do alvo, depois ainda caindo
    assert signals[3] and signals[3].side == OrderSide.BUY
