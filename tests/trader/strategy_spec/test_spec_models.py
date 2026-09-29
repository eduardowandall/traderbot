import json
import typing
from decimal import Decimal

import pytest
from factories import make_spec
from pydantic import ValidationError

from trader.strategy_spec import models
from trader.strategy_spec.conditions import PREDICATES
from trader.strategy_spec.models import StrategySpec


def _spec(**overrides) -> StrategySpec:
    return StrategySpec.model_validate(make_spec(**overrides))


def _union_types(annotated) -> set[str]:
    union = typing.get_args(annotated)[0]
    return {
        typing.get_args(cls.model_fields["type"].annotation)[0]
        for cls in typing.get_args(union)
    }


class TestParsing:
    def test_valid_spec(self):
        spec = _spec()
        assert spec.symbol == "SOL-USDC"
        assert spec.exit.stop.pct == Decimal(5)
        assert spec.entry.conditions[0].type == "price_below"

    def test_example_from_the_docs(self):
        spec = _spec(
            entry={
                "mode": "all",
                "conditions": [
                    {"type": "rsi_below", "period": 14, "value": 30},
                    {"type": "price_below_ma", "ma": "wma", "window": 50},
                ],
            },
            exit={
                "stop": {"type": "trailing_stop", "pct": 3},
                "conditions": [
                    {"type": "take_profit", "pct": 4},
                    {"type": "max_hold", "minutes": 240},
                ],
            },
        )
        assert spec.lookback() == 50
        assert spec.exit.mode == "any"

    @pytest.mark.parametrize(
        "overrides",
        [
            {"unknown_key": 1},
            {"version": 2},
            {"symbol": "sol-usdc"},
            {"name": "Bad Name"},
            {"budget_usd": 0},
            {"max_loss_usd": 60},  # acima do budget
            {"expires_at": "2099-01-01T00:00:00"},  # sem fuso
            {"supersedes": "not-hex"},
            {"sizing": {"type": "percent", "pct": 10}},
            {"entry": {"conditions": []}},
            {"entry": {"conditions": [{"type": "take_profit", "pct": 1}]}},
            {"entry": {"conditions": [{"type": "rsi_below", "value": 150}]}},
            {"entry": {"conditions": [{"type": "rsi_below", "value": 30, "x": 1}]}},
            {"exit": {"conditions": []}},  # sem stop
            {"exit": {"stop": {"type": "take_profit", "pct": 3}}},
            {"exit": {"stop": {"type": "stop_loss", "pct": 80}}},
        ],
    )
    def test_rejects_invalid_specs(self, overrides):
        with pytest.raises(ValidationError):
            _spec(**overrides)

    def test_fast_window_must_be_below_slow(self):
        condition = {"type": "fast_ma_above_slow", "fast": 50, "slow": 20}
        with pytest.raises(ValidationError, match="fast"):
            _spec(entry={"conditions": [condition]})


class TestIdentity:
    def test_id_ignores_number_formatting_and_key_order(self):
        a = _spec(budget_usd=50)
        b = StrategySpec.model_validate_json(
            json.dumps(dict(reversed(make_spec(budget_usd="50.00").items())))
        )
        assert a.spec_id() == b.spec_id()
        assert len(a.spec_id()) == 12

    def test_id_changes_with_content(self):
        assert _spec().spec_id() != _spec(budget_usd=51).spec_id()

    def test_expiry_is_normalised_to_utc(self):
        a = _spec(expires_at="2099-01-01T00:00:00Z")
        b = _spec(expires_at="2099-01-01T03:00:00+03:00")
        assert a.spec_id() == b.spec_id()


class TestSchemaAndCatalogue:
    def test_schema_lists_every_condition_type(self):
        text = json.dumps(StrategySpec.model_json_schema())
        for condition_type in PREDICATES:
            assert f'"{condition_type}"' in text

    def test_every_condition_type_has_a_predicate(self):
        declared = (
            _union_types(models.MarketCondition)
            | _union_types(models.ExitCondition)
            | _union_types(models.Stop)
        )
        assert declared == set(PREDICATES)

    def test_lookback_covers_every_condition(self):
        spec = _spec(
            entry={"conditions": [{"type": "rsi_below", "period": 20, "value": 30}]},
            exit={
                "stop": {"type": "stop_loss", "pct": 2},
                "conditions": [
                    {"type": "volatility_below", "window": 40, "pct": 1},
                ],
            },
        )
        assert spec.lookback() == 41


def test_the_documented_example_spec_parses():
    from trader.paths import PROJECT_ROOT

    example = PROJECT_ROOT / "docs" / "examples" / "spec-sol-dip.json"
    spec = StrategySpec.model_validate_json(example.read_text(encoding="utf-8"))
    assert spec.symbol == "SOL-USDC"
