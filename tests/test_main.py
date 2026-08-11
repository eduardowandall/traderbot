from decimal import Decimal

import pytest

from main import __parse_kwargs, _get_strategy_obj
from trader import NotImplementedStrategy
from trader.trading_strategy import (
    RandomStrategy,
    StrategyComposer,
    TargetValueStrategy,
)


def test_parse_kwargs_key_values():
    assert __parse_kwargs(["sell_chance=20", "buy_chance=40"]) == {
        "sell_chance": "20",
        "buy_chance": "40",
    }


def test_parse_kwargs_bare_flag():
    assert __parse_kwargs(["dry_run"]) == {"dry_run": True}


def test_get_strategy_obj_with_args():
    strategy = _get_strategy_obj("random", "sell_chance=20 buy_chance=40")
    assert isinstance(strategy, RandomStrategy)
    assert strategy.sell_chance == "20"
    assert strategy.buy_chance == "40"


def test_get_strategy_obj_no_args_uses_defaults():
    strategy = _get_strategy_obj("composer", None)
    assert isinstance(strategy, StrategyComposer)


def test_get_strategy_obj_target_value_with_args():
    strategy = _get_strategy_obj(
        "target_value", "target_buy_price=10.0 target_profit_percent=1.0"
    )
    assert isinstance(strategy, TargetValueStrategy)
    assert strategy.target_buy_price == Decimal("10.0")


def test_get_strategy_obj_unknown_strategy_raises():
    with pytest.raises(NotImplementedStrategy):
        _get_strategy_obj("does_not_exist", None)
