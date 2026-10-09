"""O bloco `market` de uma spec (A8): validação, id, termos e o `hello`."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from factories import make_spec
from pydantic import ValidationError

from trader.shared.models.direction import Direction
from trader.shared.spec.terms import SpecTerms
from trader.shared.spec.validate import SpecLimits, validate
from trader.strategy.spec.models import StrategySpec

NOW = datetime(2026, 10, 7, tzinfo=UTC)
SHORT3 = {"kind": "perp", "venue": "jupiter", "direction": "short", "leverage": 3}
PERP_EXIT = {
    "stop": {"type": "stop_loss", "pct": 5},
    "mode": "any",
    "conditions": [{"type": "max_hold", "minutes": 60}],
}
OPEN = SpecLimits(max_trade_usd=Decimal(1000), perps_enabled=True)


def _perp(**overrides) -> StrategySpec:
    fields = {"market": SHORT3, "exit": PERP_EXIT, "expires_at": None, "ttl_days": 7}
    fields |= overrides
    return StrategySpec.model_validate(make_spec(**fields))


def test_a_perp_spec_carries_its_market_into_the_terms():
    terms = _perp().terms()
    assert terms.market is not None and terms.market.direction == Direction.SHORT
    assert (terms.stop_pct, terms.max_hold_minutes) == (5, 60)
    assert terms.exposure_usd == terms.max_trade_usd * 3
    assert SpecTerms.model_validate(terms.model_dump()) == terms  # pelo fio


def test_the_market_changes_the_id_and_spot_ids_stay():
    spot = StrategySpec.model_validate(make_spec(exit=PERP_EXIT))
    assert "market" not in spot.canonical_json()
    assert _perp().spec_id() != spot.spec_id()
    long = _perp(market=SHORT3 | {"direction": "long"})
    assert long.spec_id() != _perp().spec_id()


@pytest.mark.parametrize(
    ("exit_", "message"),
    [
        ({"stop": {"type": "stop_loss", "pct": 5}}, "max_hold"),
        (PERP_EXIT | {"mode": "all"}, "max_hold"),  # com all, nunca sai sozinho
        (PERP_EXIT | {"stop": {"type": "trailing_stop", "pct": 17}}, "16.55"),
    ],
)
def test_a_perp_needs_a_max_hold_and_a_stop_well_before_liquidation(exit_, message):
    with pytest.raises(ValidationError, match=message):
        _perp(exit=exit_)


def test_the_leverage_is_bounded():
    with pytest.raises(ValidationError):
        _perp(market=SHORT3 | {"leverage": 1})


def test_a_valid_perp_passes_the_owner_limits():
    assert validate(_perp().terms(), OPEN, NOW) == []


@pytest.mark.parametrize(
    ("limits", "overrides", "message"),
    [
        (SpecLimits(max_trade_usd=Decimal(1000)), {}, "perps desligadas"),
        (
            OPEN,
            {"market": SHORT3 | {"leverage": 5}, "exit": PERP_EXIT},
            "acima do máximo",
        ),
        (OPEN, {"symbol": "JUP-USDC"}, "mercado perp não permitido"),
        (OPEN, {"symbol": "SOL-JUP"}, "colateral de uma perp é USDC"),
        (OPEN, {"symbol": "SOL-USDT"}, "colateral de uma perp é USDC"),
        # 20 de colateral a 3x são 60 de exposição: acima de 50
        (SpecLimits(Decimal(50), perps_enabled=True), {}, "60 USD acima do limite"),
    ],
)
def test_the_trade_runner_checks_the_market_against_its_policy(
    limits, overrides, message
):
    errors = validate(_perp(**overrides).terms(), limits, NOW)
    assert any(message in e.msg for e in errors), errors


# --- A12 ----------------------------------------------------------------------

PARTIAL = {"pct": 50, "conditions": [{"type": "take_profit", "pct": 2}]}
TOP_UP = {"within_pct": 10, "usd": 2, "max_times": 2}


def test_the_new_blocks_stay_out_of_the_id_until_used():
    # A12: `exit.partial` e `market.add_collateral` vazios não mudam os ids
    plain = _perp()
    assert "partial" not in plain.canonical_json()
    assert "add_collateral" not in plain.canonical_json()
    partial = _perp(exit=PERP_EXIT | {"partial": PARTIAL})
    topped = _perp(market=SHORT3 | {"add_collateral": TOP_UP})
    assert len({plain.spec_id(), partial.spec_id(), topped.spec_id()}) == 3


def test_add_collateral_reaches_the_trade_runner_as_a_top_up():
    from trader.execution.models.perp import (
        perp_terms_for,
        terms_from_json,
        terms_to_json,
    )

    terms = _perp(market=SHORT3 | {"add_collateral": TOP_UP}).terms()
    assert SpecTerms.model_validate(terms.model_dump()) == terms  # pelo fio
    perp = perp_terms_for(terms.symbol, terms.market, terms.stop_pct)
    assert perp is not None and perp.top_up is not None
    assert (perp.top_up.within_pct, perp.top_up.usd, perp.top_up.max_times) == (
        10,
        2,
        2,
    )
    assert terms_from_json(terms_to_json(perp)) == perp  # no ledger
    plain = perp_terms_for(terms.symbol, _perp().market, terms.stop_pct)
    assert plain is not None and "top_up" not in (terms_to_json(plain) or "")


@pytest.mark.parametrize(
    "partial",
    [
        {"pct": 100, "conditions": [{"type": "take_profit", "pct": 2}]},
        {"pct": 50, "conditions": []},
    ],
)
def test_a_partial_exit_sells_a_real_part_on_some_condition(partial):
    with pytest.raises(ValidationError):
        _perp(exit=PERP_EXIT | {"partial": partial})
