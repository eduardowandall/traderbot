"""O codec do `Order`: o dict do fio e o JSON do ledger (A17b)."""

import json
from dataclasses import replace
from decimal import Decimal

from factories import make_order

from trader.shared.models.costs import TradeCosts
from trader.shared.models.direction import Direction
from trader.shared.models.order import (
    order_from_dict,
    order_from_json,
    order_to_dict,
    order_to_json,
)
from trader.shared.models.perp import PerpFill

PERP = PerpFill(
    direction=Direction.SHORT,
    leverage=Decimal(2),
    price=Decimal(150),
    size_usd=Decimal(20),
    collateral_usd=Decimal(10),
    fees_usd=Decimal("0.012"),
    borrow_bps_hour=Decimal(1),
)


def _orders():
    order = make_order()
    costs = TradeCosts(source="onchain", fee_lamports=5000, rent_lamports=2039280)
    return [
        order,
        replace(order, costs=costs, closes_position=False),
        replace(order, perp=PERP),
    ]


def test_the_dict_is_plain_json_and_the_json_is_that_dict():
    for order in _orders():
        data = order_to_dict(order)
        assert json.loads(json.dumps(data)) == data  # só tipos JSON, sem enums
        assert json.loads(order_to_json(order)) == data


def test_both_forms_round_trip():
    for order in _orders():
        assert order_from_dict(order_to_dict(order)) == order
        back = order_from_json(order_to_json(order))
        assert (back.costs, back.perp) == (order.costs, order.perp)
