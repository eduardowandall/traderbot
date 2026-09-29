"""O único caminho de uma intenção até um fill com custos.

`execute_trade` passa a intenção pelo gateway (idempotência, política,
ledger, execução) e só depois busca os custos do swap. A ordem importa:
buscar custos nunca pode fazer o swap ser re-tentado ou marcado como falho,
então isso só acontece com a intenção já EXECUTED (ver AGENTS.md, custos).

Quem chama transforma o `Fill` em `Order` (só ele conhece o par e a posição)
e grava com `gateway.record_fill`.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from trader.execution.gateway import TradeGateway
from trader.models.costs import TradeCosts
from trader.models.intent import TradeIntent
from trader.models.order import SwapResult
from trader.providers.jupiter.async_jupiter_svc import AsyncJupiterProvider


@dataclass(frozen=True)
class Fill:
    result: SwapResult
    costs: TradeCosts | None = None  # None: custos desconhecidos

    def amounts(self) -> tuple[int, int]:
        """(entrada, saída) raw: efetivos quando conhecidos, senão os da quote."""
        if self.costs is None:
            return self.result.in_amount, self.result.out_amount
        in_raw = self.costs.actual_in_amount
        out_raw = self.costs.actual_out_amount
        return (
            self.result.in_amount if in_raw is None else in_raw,
            self.result.out_amount if out_raw is None else out_raw,
        )


async def execute_trade(
    gateway: TradeGateway,
    provider: AsyncJupiterProvider,
    intent: TradeIntent,
    call: Callable[[], Awaitable[SwapResult]],
) -> Fill:
    result = await gateway.submit(intent, call)
    costs = await provider.fetch_swap_costs(result)
    # providers de teste (mocks) não devolvem TradeCosts
    return Fill(result, costs if isinstance(costs, TradeCosts) else None)
