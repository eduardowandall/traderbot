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

from trader.execution.models.book import remainder_entry
from trader.execution.models.errors import failed_signatures_of
from trader.execution.models.execution import ExecutionResult
from trader.execution.models.intent import TradeIntent
from trader.execution.models.venue import PostTrade
from trader.execution.trade.gateway.gateway import TradeGateway
from trader.execution.trade.ledger.events import (
    PERP_STOP_FEE,
    PERP_STOP_PLACED,
    POSITION_LEFTOVER,
)
from trader.shared.models.costs import (
    FailedTxFee,
    PnLResult,
    TradeCosts,
    lamports_usd,
)
from trader.shared.models.mints import SOLANA_MINTS
from trader.shared.models.order import Order, order_to_json

logger = logging.getLogger(__name__)

OnFailedFee = Callable[[FailedTxFee], None]


@dataclass(frozen=True)
class Fill:
    result: ExecutionResult
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
    venue: PostTrade,
    intent: TradeIntent,
    call: Callable[[], Awaitable[ExecutionResult]],
    sol_usd: Decimal | None = None,  # do retrato de antes do trade
    on_failed_fee: OnFailedFee | None = None,
) -> Fill:
    failed = FailedFees(gateway, venue, intent, sol_usd, on_failed_fee)
    try:
        result = await gateway.submit(intent, call)
    except Exception as ex:
        await failed.book(failed_signatures_of(ex))
        raise
    return await settle(venue, result, failed, result.failed_signatures)


async def settle(
    venue: PostTrade,
    result: ExecutionResult,
    fees: FailedFees,
    failed: Sequence[str],
) -> Fill:
    """Depois de EXECUTED: os custos do swap e a taxa das tentativas falhas.

    Único caminho até `fetch_swap_costs` (o `execute_trade` e a resolução de
    intenções, A3). **Nunca** levanta.
    """
    costs = await venue.fetch_costs(result)
    await fees.book(failed)
    return Fill(result, costs)


def record_fill_safely(
    gateway: TradeGateway,
    intent_id: str,
    order: Order,
    realized_usd: Decimal | None = None,
    pnl: PnLResult | None = None,
) -> None:
    """Grava o fill; uma falha aqui não desfaz o que já foi executado.

    A intenção já está EXECUTED (com assinatura e valores), e o restore
    reconstrói a posição a partir dela mesmo sem a ordem gravada.
    """
    try:
        gateway.record_fill(intent_id, order, realized_usd, pnl)
    except Exception as ex:
        logger.error(
            f"Fill executado mas não gravado ({intent_id}): {ex}; "
            f"ordem: {order_to_json(order)}"
        )


def record_leftover(
    gateway: TradeGateway, account_id: str, entry: Order, exit_order: Order
) -> None:
    """Tokens que ficaram na carteira quando a posição fechou (ex: reserva)."""
    rest = remainder_entry(entry, exit_order.quantity)
    if rest is None:
        return
    symbol = SOLANA_MINTS[entry.output_mint].symbol
    logger.warning(f"Posição fechada com {rest.quantity} {symbol} na carteira")
    try:
        gateway.add_event(
            POSITION_LEFTOVER,
            {
                "account": account_id,
                "mint": entry.output_mint,
                "quantity": rest.quantity,
                "cost_quote": rest.quote_amount,
            },
        )
    except Exception as ex:
        logger.error(f"Sobra da posição não registrada: {ex}")


@dataclass(frozen=True)
class FailedFees:
    gateway: TradeGateway
    venue: PostTrade
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
        lamports = await self.venue.fetch_failed_fees(signatures)
        return FailedTxFee(signatures, lamports, lamports_usd(lamports, self.sol_usd))

    def _record(self, fee: FailedTxFee) -> None:
        try:
            if self.on_fee is not None:
                self.on_fee(fee)
            self.gateway.record_failed_fee(self.intent, fee)
        except Exception as ex:
            logger.error(f"Taxa de transações falhas não registrada ({fee}): {ex}")


def record_venue_order(
    gateway: TradeGateway,
    intent: TradeIntent,
    fill: Fill,
    sol_usd: Decimal | None,
    placed: str | None,
) -> Decimal | None:
    """Uma ordem no venue (o stop de uma perp, ou o cancelamento dele, A12).

    Grava a taxa do envio (`perp_stop_fee`: custo do bucket sem trade, A11c
    F2) e, num stop colocado (`placed`), o endereço dele (`perp_stop_placed`),
    para conferir depois de fechar. Devolve o USD da taxa (None: sem preço do
    SOL), que quem tem o livro desconta. **Nunca** levanta: a ordem já foi
    enviada.
    """
    lamports = fill.costs.fee_lamports if fill.costs else 0
    usd = lamports_usd(lamports, sol_usd) if lamports else Decimal(0)
    account = {"account": intent.account}
    try:
        if lamports:
            fee = {"signature": fill.result.signature, "fee_lamports": lamports}
            gateway.ledger.add_event(
                PERP_STOP_FEE,
                account | fee | {"fee_usd": usd},
                intent_id=intent.intent_id,
            )
        if placed is not None:
            gateway.ledger.add_event(
                PERP_STOP_PLACED,
                account | {"request": placed},
                intent_id=intent.intent_id,
            )
    except Exception as ex:
        logger.error(f"Ordem no venue {fill.result.signature} sem registro: {ex}")
    return usd
