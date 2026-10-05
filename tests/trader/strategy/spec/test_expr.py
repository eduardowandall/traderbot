"""A condição `expr` (B6): o parser, os erros, e a mesma resposta das tipadas."""

import random
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from factories import make_spec
from pydantic import ValidationError

from trader.shared.models.public_data import Interval
from trader.strategy.spec.conditions import IndicatorBank, TickContext, holds
from trader.strategy.spec.expr import (
    ExprError,
    history,
    lookback,
    parse,
    render,
    truth,
)
from trader.strategy.spec.models import (
    DipFromHigh,
    Expr,
    FastMaAboveSlow,
    PriceBelowMa,
    RsiBelow,
    StrategySpec,
    VolatilityBelow,
)

T0 = datetime(2026, 10, 1, tzinfo=UTC)


class TestGrammar:
    @pytest.mark.parametrize(
        ("text", "normalized"),
        [
            ("rsi(14)<30", "rsi(14) < 30"),
            ("  price <sma( 20 )*0.980 ", "price < sma(20) * 0.98"),
            ("((price < 1))", "price < 1"),
            (
                "price < 1 or price > 2 and rsi(14) < 30",
                "price < 1 or (price > 2 and rsi(14) < 30)",
            ),
            ("(price < 1 or price > 2) and rsi(14) < 30", None),
            ("not price < 1", None),
            ("-price >= -3", None),
            ("(sma(5) - sma(20)) / sma(20) > 0.01", None),
            ("price - (2 - 3) > 1 - 2 - 3", None),
            ("price / (2 * 3) > 1", None),
        ],
    )
    def test_text_is_normalized_and_round_trips(self, text, normalized):
        rendered = render(parse(text))
        assert rendered == (normalized or text)
        assert render(parse(rendered)) == rendered

    def test_and_binds_tighter_than_or(self):
        ctx = _ctx(price=Decimal(5))
        # 5 < 10 or (5 > 1 and 5 > 9) = verdadeiro; (... or ...) and 5 > 9 = falso
        assert truth(parse("price < 10 or price > 1 and price > 9"), ctx) is True
        assert truth(parse("(price < 10 or price > 1) and price > 9"), ctx) is False

    def test_arithmetic_precedence(self):
        ctx = _ctx(price=Decimal(2))
        assert truth(parse("price + 3 * 2 > 7.9"), ctx)
        assert truth(parse("(price + 3) * 2 > 9.9"), ctx)
        assert truth(parse("10 - price - 3 < 5.1"), ctx)  # (10 - 2) - 3 = 5


class TestErrors:
    @pytest.mark.parametrize(
        ("text", "message", "pos"),
        [
            ("a < 1", "nome desconhecido", 0),
            ("price", "esperava uma condição", 0),
            ("sma(1) > 2", "fora de 2-500", 4),
            ("sma(2.5) > 2", "inteiro", 4),
            ("rsi(14) < 30 and 3", "esperava uma condição", 17),
            ("price < (1 < 2)", "precisa de números", 9),
            ("1 < price < 2", "uma comparação por vez", 0),
            ("price ** 2 > 1", "não permitido", 0),
            ("price < 1 $", "sintaxe inválida", 10),
            ("sma 20 > 1", "sintaxe inválida", 4),
        ],
    )
    def test_errors_say_what_and_where(self, text, message, pos):
        with pytest.raises(ExprError, match=re.escape(message)) as raised:
            parse(text)
        assert raised.value.pos == pos

    def test_length_is_bounded(self):
        with pytest.raises(ExprError, match="caracteres"):
            parse("price < 1 and " * 20 + "price < 1")

    @pytest.mark.parametrize("text", ["price < 1 <", "price <", "(price < 1"])
    def test_syntax_errors(self, text):
        with pytest.raises(ExprError, match="sintaxe inválida"):
            parse(text)

    def test_no_python_gets_through(self):
        for text in (
            "__import__('os') < 1",
            "price.__class__ < 1",
            "price < 1; 2",
            "sma(x=3) > 1",
            "price < True",
        ):
            with pytest.raises(ExprError):
                parse(text)


class TestSpec:
    def _spec(self, expr: str) -> StrategySpec:
        entry = {"conditions": [{"type": "expr", "expr": expr}]}
        return StrategySpec.model_validate(make_spec(entry=entry))

    def test_whitespace_and_redundant_parentheses_keep_the_id(self):
        a = self._spec("rsi(14)<30 and price<sma(20)*0.98")
        b = self._spec("rsi(14) < 30 and (price < sma(20) * 0.98)")
        assert a.spec_id() == b.spec_id()
        assert (
            a.entry.conditions[0].label() == "rsi(14) < 30 and price < sma(20) * 0.98"
        )

    def test_a_bad_expression_is_a_spec_error_at_its_path(self):
        with pytest.raises(ValidationError) as raised:
            self._spec("rsi(14) <")
        (error,) = raised.value.errors()
        assert error["loc"][:3] == ("entry", "conditions", 0)
        assert "sintaxe inválida" in error["msg"]

    @pytest.mark.parametrize(
        ("text", "typed"),
        [
            ("rsi(14) < 30", RsiBelow(type="rsi_below", value=Decimal(30))),
            ("ema(20) > sma(50)", None),
            (
                "volatility(10) < 2",
                VolatilityBelow(type="volatility_below", window=10, pct=Decimal(2)),
            ),
        ],
    )
    def test_warmup_matches_the_typed_conditions(self, text, typed):
        tree = parse(text)
        if typed is not None:
            assert (lookback(tree), history(tree)) == (
                typed.lookback(),
                typed.history(),
            )
        else:
            assert (lookback(tree), history(tree)) == (50, 100)  # ema 5x vs sma 50

    def test_without_indicators_the_warmup_is_one_bar(self):
        tree = parse("price < 100")
        assert (lookback(tree), history(tree)) == (1, 1)


# --- avaliação ------------------------------------------------------------------


def _ctx(price: Decimal, bank: IndicatorBank | None = None, **extra) -> TickContext:
    return TickContext(
        price=price,
        now=T0,
        bank=bank or IndicatorBank(Interval.MINUTE_1, 50),
        **extra,
    )


EQUIVALENT = [
    ("rsi(14) < 45", RsiBelow(type="rsi_below", value=Decimal(45))),
    (
        "price < sma(20) * 0.98",
        PriceBelowMa(type="price_below_ma", window=20, pct=Decimal(2)),
    ),
    (
        "price <= high(30) * 0.98",
        DipFromHigh(type="dip_from_high", window=30, pct=Decimal(2)),
    ),
    (
        "wma(5) > wma(20)",
        FastMaAboveSlow(type="fast_ma_above_slow", ma="wma", fast=5, slow=20),
    ),
    (
        "volatility(10) < 0.85",
        VolatilityBelow(type="volatility_below", window=10, pct=Decimal("0.85")),
    ),
]


@pytest.mark.parametrize(("text", "typed"), EQUIVALENT)
def test_same_answer_as_the_typed_condition_on_every_tick(text, typed):
    condition = Expr(type="expr", expr=text)
    bank = IndicatorBank(Interval.MINUTE_1, 200)
    rng = random.Random(7)
    price = Decimal(100)
    answers = set()
    for minute in range(300):
        price *= Decimal(1) + Decimal(rng.randint(-150, 150)) / 10000
        bank.update(T0 + timedelta(minutes=minute), price)
        ctx = _ctx(price, bank)
        assert holds(condition, ctx) == holds(typed, ctx), (minute, price)
        answers.add(holds(typed, ctx))
    assert answers == {True, False}  # o teste viu os dois lados


class TestUnknownValues:
    def test_a_missing_value_never_fires(self):
        cold = _ctx(Decimal(1))  # sem barras: indicadores sem valor
        assert not holds(Expr(type="expr", expr="rsi(14) < 101"), cold)
        assert not holds(Expr(type="expr", expr="price < entry_price"), cold)
        assert not holds(Expr(type="expr", expr="price / (price - 1) > 0"), cold)

    def test_three_valued_logic(self):
        cold = _ctx(Decimal(1))
        assert truth(parse("not rsi(14) < 30"), cold) is None
        assert truth(parse("rsi(14) < 30 and price > 5"), cold) is False
        assert truth(parse("rsi(14) < 30 or price < 5"), cold) is True
        assert truth(parse("rsi(14) < 30 or price > 5"), cold) is None

    def test_position_values_come_from_the_context(self):
        ctx = _ctx(
            Decimal(108),
            entry_price=Decimal(100),
            peak=Decimal(112),
            last_exit_price=Decimal(90),
        )
        condition = (
            "price >= entry_price * 1.05 and price < peak and last_exit_price < 95"
        )
        assert holds(Expr(type="expr", expr=condition), ctx)
