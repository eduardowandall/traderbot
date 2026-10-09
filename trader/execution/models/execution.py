"""O que um local de execução devolve de uma ordem executada (A7).

Era o `SwapResult` (em `shared`): só o trade-runner e o backtest o usam. Os
campos são os mesmos, então o evento `intent_executed` e as colunas
`signature`/`in_amount`/`out_amount` do ledger não mudam. Uma perna de perp
(A8) traz também o `perp`: `in_amount`/`out_amount` são o colateral (raw do
token de cotação) e o tamanho (raw do token base), no sentido da perna.
"""

from dataclasses import dataclass, field
from typing import Any

from trader.shared.models.costs import TradeCosts
from trader.shared.models.perp import PerpFill


@dataclass
class ExecutionResult:
    """Resultado de uma ordem executada (valores raw, conforme a quote)."""

    signature: str
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    # custos já conhecidos na execução (paper/backtest); em real são
    # buscados depois, por `Venue.fetch_costs`
    costs: TradeCosts | None = field(default=None, compare=False)
    # quote usada (LP fees, impacto)
    quote: Any = field(default=None, compare=False, repr=False)
    # tentativas anteriores que a rede confirmou como falhas (taxa paga)
    failed_signatures: tuple[str, ...] = field(default=(), compare=False)
    # perna de perp: preço do oráculo, tamanho, colateral, taxas (A8)
    perp: PerpFill | None = field(default=None, compare=False)
    # a ordem que a perna deixou no venue (o endereço do stop de uma perp, A12)
    venue_order: str | None = field(default=None, compare=False)
