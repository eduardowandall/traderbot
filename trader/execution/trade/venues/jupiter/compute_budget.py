"""O orçamento de computação de uma transação nossa (A11b, A12).

Uma transação que não é da Jupiter (os pedidos de perp) paga a prioridade por
unidade de computação: o limite de unidades sai da simulação que o envio já
faz (`OnChainExecutor.send_instructions`: uma só), com folga, e o preço por
unidade sai das taxas recentes nas mesmas contas, entre um piso e o teto da
política (`max_priority_fee_lamports` com o limite inteiro).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.instruction import Instruction

MICRO_PER_LAMPORT = 1_000_000
# o limite da primeira assinatura (a da simulação); um pedido de perp usa ~95 mil
MAX_UNITS = 200_000
# o limite final: o simulado com folga, nunca abaixo disto
MIN_UNITS = 100_000
UNITS_MARGIN = Decimal("1.3")
# o preço por unidade nunca fica abaixo desta fração do teto: um pedido
# parado na fila é pior que pagar um pouco de prioridade
PRIORITY_FLOOR = Decimal("0.25")


@dataclass(frozen=True)
class ComputeBudget:
    """Como pagar a prioridade de uma transação nossa.

    `recent`: o preço por unidade (micro-lamports) que as transações recentes
    nas mesmas contas pagaram; None (não deu para ler): o teto.
    """

    recent: int | None = None


def unit_limit(units: int | None) -> int:
    """O limite de unidades: as simuladas com folga; sem elas, o máximo."""
    if not units:
        return MAX_UNITS
    return max(int(units * UNITS_MARGIN), MIN_UNITS)


def unit_price(cap_lamports: int, limit: int, recent: int | None) -> int:
    """O preço recente entre o piso e o teto (a prioridade nunca passa de
    `cap_lamports` com o limite inteiro); sem ele, o teto."""
    cap = cap_lamports * MICRO_PER_LAMPORT // limit
    if recent is None:
        return cap
    return min(max(recent, int(cap * PRIORITY_FLOOR)), cap)


def budgeted(
    instructions: Sequence[Instruction], limit: int, micro_price: int
) -> list[Instruction]:
    """As instruções com o limite de unidades e o preço por unidade na frente."""
    return [
        set_compute_unit_limit(limit),
        set_compute_unit_price(micro_price),
        *instructions,
    ]
