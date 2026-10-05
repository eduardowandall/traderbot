"""B13: os termos que o trade-runner recebe no lugar da spec inteira."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from factories import make_spec, terms_of
from pydantic import ValidationError

from trader.shared.spec.terms import SpecTerms
from trader.strategy.spec.models import StrategySpec

T0 = datetime(2026, 10, 5, tzinfo=UTC)


def test_fixed_sizing_terms_carry_the_money_and_the_id():
    spec = StrategySpec.model_validate(make_spec())
    terms = spec.terms()
    assert terms.spec_id == spec.spec_id()
    assert (terms.name, terms.symbol) == ("sol-dip", "SOL-USDC")
    assert (terms.budget_usd, terms.max_loss_usd) == (Decimal(50), Decimal(10))
    assert (terms.max_trade_usd, terms.sizing_field) == (Decimal(20), "sizing.usd")
    assert terms.expiry(T0) == datetime(2099, 1, 1, tzinfo=UTC)


def test_pct_sizing_terms_use_the_largest_buy():
    terms = terms_of(
        make_spec(sizing={"type": "pct_of_bucket", "pct": 40}, budget_usd=50)
    )
    assert (terms.max_trade_usd, terms.sizing_field) == (Decimal(20), "sizing.pct")


def test_ttl_terms_expire_from_the_start():
    terms = terms_of(make_spec(ttl_days=3, expires_at=None))
    assert terms.expiry(T0) == T0 + timedelta(days=3)


def test_terms_survive_the_wire():
    terms = terms_of(make_spec(ttl_days=3, expires_at=None))
    assert SpecTerms.model_validate(terms.model_dump(mode="json")) == terms


def test_terms_from_the_wire_keep_the_spec_invariants():
    data = terms_of(make_spec()).model_dump(mode="json")
    with pytest.raises(ValidationError, match="max_loss_usd"):
        SpecTerms.model_validate({**data, "max_loss_usd": "60"})
    with pytest.raises(ValidationError, match="exatamente um"):
        SpecTerms.model_validate({**data, "ttl_days": 3})
    with pytest.raises(ValidationError):
        SpecTerms.model_validate({**data, "entry": {}})  # a spec inteira não passa
