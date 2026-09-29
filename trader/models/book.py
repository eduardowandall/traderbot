"""O livro de um bucket: a posição aberta e o PnL realizado.

Só dados e contas (camada core). Quem executa (`AsyncAccount`) abre e
fecha posições aqui; o ledger é a fonte da verdade, e após um reinício o
livro é reconstruído com `PositionBook.restored(...)`.

O PnL fica em duas formas:
- nativo, no token de cotação (bruto, líquido e custos em SOL): a verdade;
- `realized_usd`, a estimativa líquida em USD usada pelo orçamento do bucket.
"""

from dataclasses import dataclass
from decimal import Decimal

from trader.models.costs import PnLResult
from trader.models.order import Order
from trader.models.position import Position, PositionType

ZERO = Decimal("0")


@dataclass(frozen=True)
class ClosedPosition:
    position: Position  # com a ordem de saída
    pnl: PnLResult | None  # None: ordens sem valores nativos
    realized_usd: Decimal


@dataclass
class PositionBook:
    quote_symbol: str
    position: Position | None = None
    realized_usd: Decimal = ZERO
    gross_quote: Decimal = ZERO
    net_quote: Decimal = ZERO
    costs_sol: Decimal = ZERO
    incomplete: int = 0  # posições fechadas sem custos convertidos

    @classmethod
    def restored(
        cls,
        quote_symbol: str,
        realized_usd: Decimal,
        gross_quote: Decimal,
        net_quote: Decimal,
        costs_sol: Decimal,
        incomplete: int,
        entry: Order | None,
    ) -> PositionBook:
        """O livro reconstruído a partir do ledger (totais + entrada aberta)."""
        book = cls(
            quote_symbol,
            realized_usd=realized_usd,
            gross_quote=gross_quote,
            net_quote=net_quote,
            costs_sol=costs_sol,
            incomplete=incomplete,
        )
        if entry is not None:
            book.open(entry)
        return book

    def open(self, entry: Order) -> Position:
        if self.position is not None:
            raise ValueError("já existe uma posição aberta")
        self.position = Position(PositionType.LONG, entry_order=entry, exit_order=None)
        return self.position

    def close(self, exit_order: Order) -> ClosedPosition:
        position = self.position
        if position is None:
            raise ValueError("não há posição aberta para fechar")
        position.exit_order = exit_order
        closed = ClosedPosition(
            position, position.realized_pnl_detail(), position.realized_pnl
        )
        self.realized_usd += closed.realized_usd
        self._book(closed.pnl)
        self.position = None
        return closed

    def _book(self, pnl: PnLResult | None) -> None:
        """Acumula o PnL nativo de uma posição fechada."""
        if pnl is None:
            self.incomplete += 1
            return
        self.gross_quote += pnl.gross_quote
        self.costs_sol += pnl.costs_sol
        net = pnl.net_quote
        self.net_quote += pnl.gross_quote if net is None else net
        self.incomplete += 0 if pnl.complete else 1

    def summary(self) -> str:
        """PnL realizado: líquido nativo, estimativa USD, bruto e custos."""
        flag = (
            f" [!] {self.incomplete} trade(s) incompleto(s)" if self.incomplete else ""
        )
        return (
            f"PNL líquido {self.net_quote:+.6f} {self.quote_symbol} "
            f"(~${self.realized_usd:+.4f}); bruto {self.gross_quote:+.6f}, "
            f"custos {self.costs_sol:.9f} SOL{flag}"
        )
