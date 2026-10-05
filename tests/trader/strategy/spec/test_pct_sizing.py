"""Sizing (B6): percentual do bucket, e `fixed_usd` convertido para a cotação."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from factories import make_spec

from trader.shared.spec.models import StrategySpec
from trader.shared.spec.validate import SpecLimits, validate
from trader.strategy.spec.strategy import SpecStrategy

T0 = datetime(2026, 10, 1, tzinfo=UTC)
ALWAYS = {"conditions": [{"type": "price_below", "value": 1000}]}


def _strategy(sizing: dict) -> SpecStrategy:
    spec = StrategySpec.model_validate(make_spec(entry=ALWAYS, sizing=sizing))
    strategy = SpecStrategy(spec)
    strategy.set_clock(lambda: T0 + timedelta(minutes=1))
    return strategy


def _bought(strategy, price, available, quote_usd=Decimal(1)):
    signal = strategy.on_market_refresh(
        Decimal(price), Decimal(available), None, quote_usd=quote_usd
    )
    assert signal is not None
    return signal.quantity * Decimal(price)  # o gasto, em cotação


def test_pct_of_bucket_spends_a_share_of_what_the_bucket_may_spend():
    strategy = _strategy({"type": "pct_of_bucket", "pct": 25})
    assert _bought(strategy, "50", "40") == Decimal(10)


def test_fixed_usd_is_converted_to_the_quote_token():
    # JUP-SOL a 200 USD/SOL: 20 USD são 0.1 SOL
    strategy = _strategy({"type": "fixed_usd", "usd": 20})
    assert _bought(strategy, "0.005", "3", quote_usd=Decimal(200)) == Decimal("0.1")
    # nunca mais do que o bucket pode gastar
    assert _bought(_strategy({"type": "fixed_usd", "usd": 20}), "1", "7") == 7


def test_fixed_usd_without_the_quote_price_does_not_buy():
    strategy = _strategy({"type": "fixed_usd", "usd": 20})
    assert strategy.on_market_refresh(Decimal(1), Decimal(7), None, None) is None


def test_the_worst_case_of_a_percent_must_fit_the_trade_limit():
    spec = StrategySpec.model_validate(
        make_spec(
            sizing={"type": "pct_of_bucket", "pct": 50},
            budget_usd=100,
            expires_at=None,
            ttl_days=10,
        )
    )
    errors = validate(spec, SpecLimits(max_trade_usd=Decimal(25)))
    assert [(e.path, e.msg) for e in errors] == [
        ("sizing.pct", "50 USD acima do limite por trade 25")
    ]
    assert validate(spec, SpecLimits(max_trade_usd=Decimal(50))) == []


def test_percent_is_bounded():
    for pct in (0, 101):
        try:
            StrategySpec.model_validate(
                make_spec(sizing={"type": "pct_of_bucket", "pct": pct})
            )
        except ValueError:
            continue
        raise AssertionError(f"pct {pct} aceito")
