"""Estratégias legadas: dimensionamento explícito e log só em mudanças."""

import logging
from decimal import Decimal

from trader.trading_strategy import (
    StrategyComposer,
    TargetPercentStrategy,
    TargetValueStrategy,
    TrailingStopLossStrategy,
    WeightedMovingAverageStrategy,
)

TEN = Decimal("10")


def _strategy_logs(caplog):
    return [r for r in caplog.records if r.name == "trader.trading_strategy"]


class TestOrderUsd:
    def test_default_keeps_the_old_five_unit_cap(self):
        for cls in (TrailingStopLossStrategy, TargetPercentStrategy):
            assert cls().calculate_quantity(Decimal("100"), TEN) == Decimal("0.5")

    def test_is_configurable(self):
        strategy = TargetValueStrategy("10", "5", order_usd="20")
        assert strategy.calculate_quantity(Decimal("100"), TEN) == Decimal("2")

    def test_below_the_cap_uses_the_balance_percent(self):
        strategy = TrailingStopLossStrategy(order_usd="20", balance_percent="50")
        assert strategy.calculate_quantity(Decimal("10"), TEN) == Decimal("0.5")


class TestLogsOnlyOnChange:
    def test_target_value_is_quiet_while_waiting(self, caplog):
        caplog.set_level(logging.DEBUG)
        strategy = TargetValueStrategy("10", "5")

        for price in ("20", "21", "19", "22"):
            strategy.on_market_refresh(Decimal(price), None, Decimal("100"), None)

        assert len(_strategy_logs(caplog)) == 1

    def test_wma_logs_when_its_verdict_flips(self, caplog):
        caplog.set_level(logging.DEBUG)
        strategy = WeightedMovingAverageStrategy(short_window=2, long_window=3)
        strategy.set_clock(lambda: strategy.last_price_time)  # um bar só: sem append

        prices = ["10", "10", "10"]
        strategy.price_history = [Decimal(p) for p in prices]
        for price in ("10", "10", "5", "5", "20"):
            strategy.on_market_refresh(Decimal(price), None, Decimal("100"), None)

        messages = [r.getMessage() for r in _strategy_logs(caplog)]
        assert messages == ["(S2 L3 B = NOK)", "(S2 L3 B = OK)", "(S2 L3 B = NOK)"]

    def test_composer_logs_hold_once(self, caplog):
        caplog.set_level(logging.DEBUG)
        composer = StrategyComposer()

        for _ in range(5):
            composer.on_market_refresh(TEN, None, Decimal("100"), None)

        holds = [r for r in _strategy_logs(caplog) if r.getMessage() == "[HOLD]"]
        assert len(holds) == 1
