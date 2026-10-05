import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from factories import make_spec

from trader.shared.spec.validate import SpecLimits, validate
from trader.strategy.spec.models import StrategySpec
from trader.strategy.spec.parse import SpecParseError, parse_spec

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
LIMITS = SpecLimits(max_trade_usd=Decimal(25))


def _errors(limits=LIMITS, **overrides):
    overrides.setdefault("expires_at", "2026-10-10T00:00:00Z")
    spec = StrategySpec.model_validate(make_spec(**overrides))
    return [(e.path, e.msg) for e in validate(spec.terms(), limits, NOW)]


def test_valid_spec_has_no_errors():
    assert _errors() == []


@pytest.mark.parametrize(
    ("symbol", "fragment"),
    [
        ("NOPE-USDC", "não existe"),
        ("SOL-SOL", "mesmo token"),
        ("USDT-USDC", "não pode ser stablecoin"),
        ("USDC-SOL", "não pode ser stablecoin"),
    ],
)
def test_symbol_rules(symbol, fragment):
    errors = _errors(symbol=symbol)
    assert errors and all(path == "symbol" for path, _ in errors)
    assert any(fragment in msg for _, msg in errors)


def test_any_registry_token_can_be_the_input():
    # B6: o preço é no token de cotação e o orçamento em USD é convertido
    assert _errors(symbol="JUP-SOL") == []


def test_allow_list_applies_to_both_legs():
    limits = SpecLimits(max_trade_usd=Decimal(25), allowed_symbols=("SOL",))
    assert _errors(limits) == [("symbol", "símbolo não permitido: USDC")]


def test_sizing_above_the_policy_trade_limit_is_rejected():
    # senão toda compra seria recusada em silêncio pela política
    errors = _errors(sizing={"type": "fixed_usd", "usd": 30})
    assert errors == [("sizing.usd", "30 USD acima do limite por trade 25")]


def test_sizing_above_budget_is_rejected():
    limits = SpecLimits(max_trade_usd=Decimal(1000))
    errors = _errors(
        limits, sizing={"type": "fixed_usd", "usd": 60}, budget_usd=50, max_loss_usd=5
    )
    assert errors == [("sizing.usd", "60 USD acima do budget_usd 50")]


@pytest.mark.parametrize(
    ("expires_at", "fragment"),
    [("2026-09-29T11:00:00Z", "já expirou"), ("2026-12-01T00:00:00Z", "30 dias")],
)
def test_expiry_window(expires_at, fragment):
    errors = _errors(expires_at=expires_at)
    assert [path for path, _ in errors] == ["expires_at"]
    assert fragment in errors[0][1]


def test_parse_errors_point_to_the_field():
    text = make_spec(exit={"stop": {"type": "stop_loss", "pct": 99}})
    with pytest.raises(SpecParseError) as ex:
        parse_spec(json.dumps(text))
    assert [e.path for e in ex.value.errors] == ["exit.stop.stop_loss.pct"]


def test_parse_rejects_non_json():
    with pytest.raises(SpecParseError):
        parse_spec("not json")
