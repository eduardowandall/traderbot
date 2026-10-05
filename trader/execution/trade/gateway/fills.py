"""O único caminho de uma intenção até um fill com custos.

`execute_trade` passa a intenção pelo gateway (idempotência, política,
ledger, execução) e só depois busca os custos do swap. A ordem importa:
buscar custos nunca pode fazer o swap ser re-tentado ou marcado como falho,
então isso só acontece com a intenção já EXECUTED (ver AGENTS.md, custos).

Tentativas que a rede confirmou como falhas pagaram taxa: com o swap
executado ou não, a taxa é lida e registrada (evento `failed_tx_fee` e
`on_failed_fee`, que desconta do PnL do bucket). Isso nunca levanta.

Quem chama transforma o `Fill` em `Order` (só ele conhece o par e a posição)
e grava com `gateway.record_fill`.
"""

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from trader.execution.models.errors import failed_signatures_of
from trader.execution.models.intent import TradeIntent
from trader.execution.trade.gateway.gateway import TradeGateway
from trader.execution.trade.venues.jupiter.async_jupiter_svc import AsyncJupiterProvider
from trader.shared.models.costs import LAMPORTS_PER_SOL, FailedTxFee, TradeCosts
from trader.shared.models.order import SwapResult

logger = logging.getLogger(__name__)

OnFailedFee = Callable[[FailedTxFee], None]


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
    sol_usd: Decimal | None = None,  # do retrato de antes do trade
    on_failed_fee: OnFailedFee | None = None,
) -> Fill:
    failed = _FailedFees(gateway, provider, intent, sol_usd, on_failed_fee)
    try:
        result = await gateway.submit(intent, call)
    except Exception as ex:
        await failed.book(failed_signatures_of(ex))
        raise
    costs = await provider.fetch_swap_costs(result)
    await failed.book(result.failed_signatures)
    return Fill(result, costs)


@dataclass(frozen=True)
class _FailedFees:
    gateway: TradeGateway
    provider: AsyncJupiterProvider
    intent: TradeIntent
    sol_usd: Decimal | None
    on_fee: OnFailedFee | None

    async def book(self, signatures: Sequence[str]) -> None:
        """Registra a taxa das transações falhas. **Nunca** levanta."""
        if not signatures:
            return
        try:
            fee = await self._fee(tuple(signatures))
        except Exception as ex:
            logger.error(f"Taxa de transações falhas não lida {signatures}: {ex}")
            return
        logger.warning(
            f"{len(signatures)} transação(ões) falha(s) em {self.intent.intent_id}: "
            f"taxa {fee.sol} SOL"
        )
        self._record(fee)

    async def _fee(self, signatures: tuple[str, ...]) -> FailedTxFee:
        lamports = await self.provider.fetch_failed_fees(signatures)
        usd = None
        if self.sol_usd is not None:
            usd = Decimal(lamports) / LAMPORTS_PER_SOL * self.sol_usd
        return FailedTxFee(signatures, lamports, usd)

    def _record(self, fee: FailedTxFee) -> None:
        try:
            if self.on_fee is not None:
                self.on_fee(fee)
            self.gateway.record_failed_fee(self.intent, fee)
        except Exception as ex:
            logger.error(f"Taxa de transações falhas não registrada ({fee}): {ex}")
