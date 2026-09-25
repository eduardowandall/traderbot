from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum, auto

from trader.models.costs import PnLResult
from trader.models.mints import SOLANA_MINTS
from trader.models.order import Order

ZERO = Decimal("0")


def _times(amount: Decimal, rate: Decimal | None) -> Decimal | None:
    """amount x rate; zero dispensa a taxa, taxa desconhecida dá None."""
    if not amount:
        return ZERO
    return None if rate is None else amount * rate


def _minus(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    return None if a is None or b is None else a - b


def _plus(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    return None if a is None or b is None else a + b


def _leg_costs(order: Order, fraction: Decimal):
    """(SOL, em cotação, em USD, conhecido) dos custos de uma perna."""
    costs = order.costs
    sol = costs.native_cost_sol * fraction if costs else ZERO
    known = costs is not None and costs.known
    return sol, _times(sol, order.sol_in_quote), _times(sol, order.sol_usd), known


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
        """PnL realizado em USD (estimativa líquida quando há dados nativos)."""
        detail = self.realized_pnl_detail()
        if detail is not None and detail.net_usd is not None:
            return detail.net_usd
        if self.exit_order:
            # ordens antigas: preço USD x quantidade vendida
            return (
                self.exit_order.price - self.entry_order.price
            ) * self.exit_order.quantity
        return Decimal("0.0")

    def realized_pnl_detail(self) -> PnLResult | None:
        """PnL nativo (token de cotação) e custos, a partir dos valores efetivos."""
        entry, exit_ = self.entry_order, self.exit_order
        if exit_ is None or entry.quote_amount is None or exit_.quote_amount is None:
            return None
        # a venda pode ser menor que a entrada (limitada ao saldo)
        fraction = min(exit_.quantity / entry.quantity, Decimal("1"))
        spent = entry.quote_amount * fraction
        entry_costs = _leg_costs(entry, fraction)
        exit_costs = _leg_costs(exit_, Decimal("1"))
        return PnLResult(
            quote_symbol=SOLANA_MINTS.symbol_of(entry.input_mint),
            gross_quote=exit_.quote_amount - spent,
            costs_sol=entry_costs[0] + exit_costs[0],
            costs_quote=_plus(entry_costs[1], exit_costs[1]),
            gross_usd=_minus(
                _times(exit_.quote_amount, exit_.quote_usd),
                _times(spent, entry.quote_usd),
            ),
            costs_usd=_plus(entry_costs[2], exit_costs[2]),
            complete=_complete(entry_costs, exit_costs),
        )

    def unrealized_pnl_percent(self, current_price: Decimal) -> Decimal:
        """Calcula o PnL não realizado em percentual"""
        pnl_value = self.unrealized_pnl(current_price)
        return (
            pnl_value / (self.entry_order.price * self.entry_order.quantity)
        ) * Decimal("100.0")

    @property
    def realized_pnl_percent(self) -> Decimal:
        if not self.exit_order:
            return Decimal("0.0")
        return (
            (self.exit_order.price - self.entry_order.price) / self.entry_order.price
        ) * Decimal("100.0")

    def __eq__(self, value):
        return (
            value
            and self.entry_order == value.entry_order
            and self.exit_order == value.exit_order
        )


def _complete(entry_costs, exit_costs) -> bool:
    return all(
        (
            entry_costs[3],
            exit_costs[3],
            entry_costs[1] is not None,
            exit_costs[1] is not None,
        )
    )
