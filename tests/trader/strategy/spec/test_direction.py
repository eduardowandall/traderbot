"""Os blocos de posição nos dois lados (A7, D3).

Comprado é o spot de sempre (os números são os de `test_spec_strategy.py`);
vendido ganha quando o preço cai: o pico é o menor preço desde a entrada, e
alvo e stop trocam de lado.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from factories import make_spec

from trader.shared.models import SOLANA_MINTS, Order, OrderSide, Position
from trader.shared.models.direction import Direction
from trader.shared.trading_service.wire import position_from_dict, position_to_dict
from trader.strategy.spec.models import StrategySpec
from trader.strategy.spec.strategy import SpecStrategy

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
USDC = SOLANA_MINTS.get_by_symbol("USDC").mint
SOL = SOLANA_MINTS.get_by_symbol("SOL").mint
LONG, SHORT = Direction.LONG, Direction.SHORT
WIDE_STOP = {"type": "stop_loss", "pct": 50}


def _position(direction: Direction, price="100") -> Position:
    order = Order("buy-1", USDC, SOL, Decimal("0.2"), Decimal(price), OrderSide.BUY, T0)
    return Position(order, None, direction)


def _exits(exit_: dict, direction: Direction, prices: list[str]) -> list[bool]:
    """Um sinal de saída em cada tick? (mesma posição, um minuto por tick)"""
    strategy = SpecStrategy(StrategySpec.model_validate(make_spec(exit=exit_)))
    clock = [T0]
    strategy.set_clock(lambda: clock[0])
    position = _position(direction)
    fired = []
    for price in prices:
        signal = strategy.on_market_refresh(
            Decimal(price), Decimal(100), position, Decimal(1)
        )
        fired.append(signal is not None and signal.side == OrderSide.SELL)
        clock[0] += timedelta(minutes=1)
    return fired


STOP5 = {"stop": {"type": "stop_loss", "pct": 5}}
TRAIL3 = {"stop": {"type": "trailing_stop", "pct": 3}}
TP10 = {"stop": WIDE_STOP, "conditions": [{"type": "take_profit", "pct": 10}]}
TTP10 = {
    "stop": WIDE_STOP,
    "conditions": [{"type": "trailing_take_profit", "pct": 10, "trail_pct": 2}],
}
CASES = [
    # bloco, preços comprado, preços vendido, quais disparam
    (STOP5, ["96", "95"], ["104", "105"], [0, 1]),
    # pico 110 (comprado) / 90 (vendido); o trailing segue o melhor preço
    (TRAIL3, ["110", "107", "106.7"], ["90", "92", "92.7"], [0, 0, 1]),
    # o pico começa na entrada: um primeiro tick contra já respeita o stop
    (TRAIL3, ["97"], ["103"], [1]),
    (TP10, ["109", "110"], ["91", "90"], [0, 1]),
    # arma no alvo (110 / 90) e vende ao devolver 2% do pico
    (
        TTP10,
        ["109", "107", "110", "107.9", "107.8"],
        ["91", "93", "90", "91.7", "91.8"],
        [0, 0, 0, 0, 1],
    ),
]


@pytest.mark.parametrize("direction", [LONG, SHORT])
@pytest.mark.parametrize(("exit_", "long", "short", "expected"), CASES)
def test_position_blocks_compare_in_the_favourable_direction(
    direction, exit_, long, short, expected
):
    prices = long if direction == LONG else short
    assert _exits(exit_, direction, prices) == [bool(e) for e in expected]


@pytest.mark.parametrize(
    ("direction", "pnl"), [(LONG, Decimal("2")), (SHORT, Decimal("-2"))]
)
def test_unrealized_pnl_is_signed_by_the_direction(direction, pnl):
    position = _position(direction)
    assert position.unrealized_pnl(Decimal(110)) == pnl
    assert position.unrealized_pnl_percent(Decimal(110)) == pnl * 5


def test_direction_helpers():
    assert (LONG.sign, SHORT.sign) == (1, -1)
    assert LONG.better(Decimal(1), Decimal(2)) == 2
    assert SHORT.better(Decimal(1), Decimal(2)) == 1


def test_the_wire_sends_the_direction_only_when_it_is_not_long():
    # um `connect` de antes do A7 segue lendo uma posição comprada
    long, short = _position(LONG), _position(SHORT)
    sent_long, sent_short = position_to_dict(long), position_to_dict(short)
    assert sent_long is not None and sent_short is not None
    assert "direction" not in sent_long
    assert sent_short["direction"] == "short"
    assert position_from_dict(sent_long) == long
    assert position_from_dict(sent_short) == short
