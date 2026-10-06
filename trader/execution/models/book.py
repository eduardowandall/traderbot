"""O livro de um bucket: a posição aberta e o PnL realizado.

Só dados e contas (camada core). Quem executa (`AsyncAccount`) abre e
fecha posições aqui; o ledger é a fonte da verdade, e após um reinício o
livro é reconstruído com `PositionBook.restored(...)`.

O PnL fica em duas formas:
- nativo, no token de cotação (bruto, líquido e custos em SOL): a verdade;
- `realized_usd`, a estimativa líquida em USD usada pelo orçamento do bucket.
"""

from dataclasses import dataclass, replace
from decimal import Decimal

from trader.shared.models.costs import FailedTxFee, PnLResult, TradeCosts
from trader.shared.models.order import Order
from trader.shared.models.position import Position

ZERO = Decimal("0")
# resto abaixo desta fração da entrada é poeira: a posição fecha
DUST_FRACTION = Decimal("0.01")


def remainder_entry(entry: Order, sold: Decimal) -> Order | None:
    """A entrada do que sobra depois de vender `sold`; None se nada (ou poeira).

    Quantidade, valor em cotação e custos são proporcionais ao que sobrou.
    """
    remaining = entry.quantity - sold
    if entry.quantity <= 0 or remaining <= entry.quantity * DUST_FRACTION:
        return None
    share = remaining / entry.quantity
    quote_amount = entry.quote_amount
    return replace(
        entry,
        quantity=remaining,
        quote_amount=None if quote_amount is None else quote_amount * share,
        costs=_scaled(entry.costs, share),
    )


def _scaled(costs: TradeCosts | None, share: Decimal) -> TradeCosts | None:
    if costs is None:
        return None
    return replace(
        costs,
        fee_lamports=int(costs.fee_lamports * share),
        priority_fee_lamports=int(costs.priority_fee_lamports * share),
        rent_lamports=int(costs.rent_lamports * share),
        other_lamports=int(costs.other_lamports * share),
        # valores efetivos são do swap inteiro: não valem para uma fração
        actual_in_amount=None,
        actual_out_amount=None,
        quoted_out_amount=None,
    )


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
    # taxas de transações que falharam na rede (já descontadas de realized_usd)
    failed_fee_sol: Decimal = ZERO
    # rent devolvido ao fechar a conta do token (o líquido da taxa já está em
    # realized_usd; A15)
    rent_refund_sol: Decimal = ZERO

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
        failed_fee_sol: Decimal = ZERO,
        rent_refund_sol: Decimal = ZERO,
    ) -> PositionBook:
        """O livro reconstruído a partir do ledger (totais + entrada aberta)."""
        book = cls(
            quote_symbol,
            realized_usd=realized_usd,
            gross_quote=gross_quote,
            net_quote=net_quote,
            costs_sol=costs_sol,
            incomplete=incomplete,
            failed_fee_sol=failed_fee_sol,
            rent_refund_sol=rent_refund_sol,
        )
        if entry is not None:
            book.open(entry)
        return book

    def open(self, entry: Order) -> Position:
        if self.position is not None:
            raise ValueError("já existe uma posição aberta")
        self.position = Position(entry)
        return self.position

    def close(self, exit_order: Order) -> ClosedPosition:
        """Fecha a posição inteira (o PnL conta só a fração vendida)."""
        closed = self._realize(exit_order)
        self.position = None
        return closed

    def reduce(self, exit_order: Order) -> ClosedPosition:
        """Venda parcial: realiza a fração vendida e mantém o resto aberto."""
        entry = self._open().entry_order
        closed = self._realize(exit_order)
        rest = remainder_entry(entry, exit_order.quantity)
        self.position = None if rest is None else Position(rest)
        return closed

    def settle_sell(
        self, exit_order: Order, may_keep_rest: bool = True
    ) -> tuple[Order, ClosedPosition]:
        """Fecha a posição, ou só reduz se pode sobrar algo que não é poeira.

        `may_keep_rest` é falso só para intenções antigas, que já fechavam a
        posição (limitadas ao saldo, antes da A5). Devolve a ordem com
        `closes_position` decidido e o que foi realizado.
        """
        entry = self._open().entry_order
        keep_rest = (
            may_keep_rest and remainder_entry(entry, exit_order.quantity) is not None
        )
        exit_order = replace(exit_order, closes_position=not keep_rest)
        closed = self.reduce(exit_order) if keep_rest else self.close(exit_order)
        return exit_order, closed

    def charge(self, fee: FailedTxFee) -> None:
        """Taxa de transações que falharam: sai do PnL (e do orçamento)."""
        self.failed_fee_sol += fee.sol
        self.realized_usd -= fee.usd or ZERO

    def _open(self) -> Position:
        if self.position is None:
            raise ValueError("não há posição aberta para fechar")
        return self.position

    def _realize(self, exit_order: Order) -> ClosedPosition:
        position = self._open()
        position.exit_order = exit_order
        detail = position.realized_pnl_detail()
        closed = ClosedPosition(position, detail, position.realized_usd(detail))
        self.realized_usd += closed.realized_usd
        self._book(closed.pnl)
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
        extra = (
            f", tx falhas {self.failed_fee_sol:.9f} SOL" if self.failed_fee_sol else ""
        )
        if self.rent_refund_sol:
            extra += f", rent devolvido {self.rent_refund_sol:+.9f} SOL"
        return (
            f"PNL líquido {self.net_quote:+.6f} {self.quote_symbol} "
            f"(~${self.realized_usd:+.4f}); bruto {self.gross_quote:+.6f}, "
            f"custos {self.costs_sol:.9f} SOL{extra}{flag}"
        )
