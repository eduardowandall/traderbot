from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum, auto


class OrderSide(StrEnum):
    BUY = auto()
    SELL = auto()


@dataclass
class OrderSignal:
    side: OrderSide
    quantity: Decimal


@dataclass
class Order:
    order_id: str
    symbol: str
    quantity: Decimal
    price: Decimal
    side: OrderSide
    timestamp: datetime

    def __eq__(self, value):
        return (
            value
            and self.order_id == value.order_id
            and self.symbol == value.symbol
            and self.quantity == value.quantity
            and self.price == value.price
            and self.side == value.side
            and self.timestamp == value.timestamp
        )
