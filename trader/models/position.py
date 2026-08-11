from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum, auto

from trader.models.order import Order


class PositionType(StrEnum):
    LONG = auto()


@dataclass
class Position:
    """Representa uma posição de trading"""

    type: PositionType
    entry_order: Order
    exit_order: Order | None

    def unrealized_pnl(self, current_price: Decimal) -> Decimal:
        """Calcula o PnL não realizado"""
        return (current_price - self.entry_order.price) * self.entry_order.quantity

    @property
    def realized_pnl(self) -> Decimal:
        """Calcula o PnL realizado"""
        if self.exit_order:
            return (
                self.exit_order.price - self.entry_order.price
            ) * self.entry_order.quantity
        return Decimal("0.0")

    def unrealized_pnl_percent(self, current_price: Decimal) -> Decimal:
        """Calcula o PnL não realizado em percentual"""
        pnl_value = self.unrealized_pnl(current_price)
        return (
            pnl_value / (self.entry_order.price * self.entry_order.quantity)
        ) * Decimal("100.0")

    @property
    def realized_pnl_percent(self) -> Decimal:
        pnl_value = self.realized_pnl
        return (
            pnl_value / (self.entry_order.price * self.entry_order.quantity)
        ) * Decimal("100.0")

    def __eq__(self, value):
        return (
            value
            and self.entry_order == value.entry_order
            and self.exit_order == value.exit_order
        )
