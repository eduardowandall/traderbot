"""A ordem de uma intenção resolvida à mão como executada (`ledger resolve`).

Sem ela, uma compra resolvida não reabre a posição (e a estratégia compra de
novo) e uma venda resolvida deixa a posição antiga aberta. Em modo real os
valores e as taxas vêm da transação confirmada (`fetch_costs`); sem ela, dos
valores pedidos na intenção, com custos desconhecidos.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal

from trader.execution.fills import Fill
from trader.execution.orders import order_from_fill
from trader.models import SOLANA_MINTS, Mint, Order, OrderSide
from trader.models.book import PositionBook
from trader.models.costs import PnLResult, TradeCosts
from trader.models.intent import IntentRecord, IntentSide
from trader.models.order import SwapResult

FetchCosts = Callable[[SwapResult], Awaitable[TradeCosts | None]]


async def resolved_fill(
    record: IntentRecord,
    entry: Order | None,
    now: datetime,
    fetch_costs: FetchCosts | None = None,
) -> tuple[Order, Decimal | None, PnLResult | None]:
    """(ordem, PnL realizado em USD, PnL nativo): o PnL só em vendas."""
    intent = record.intent
    spend, receive = SOLANA_MINTS[intent.spend_mint], SOLANA_MINTS[intent.receive_mint]
    result = SwapResult(
        record.signature or intent.intent_id,
        spend.mint,
        receive.mint,
        spend.ui_to_raw(intent.spend_amount),
        _estimated_out(record, spend, receive),
    )
    costs = await fetch_costs(result) if fetch_costs and record.signature else None
    quote, token, side = _pair(intent.side, spend, receive)
    order = order_from_fill(
        Fill(result, costs if isinstance(costs, TradeCosts) else None),
        quote,
        token,
        side,
        now,
        signal_price=intent.price,
        requested_quantity=intent.quantity,
    )
    if side != OrderSide.SELL or entry is None:
        return order, None, None
    book = PositionBook(quote.symbol)
    book.open(entry)
    closed = book.close(order)
    return order, closed.realized_usd, closed.pnl


def _pair(side: IntentSide, spend: Mint, receive: Mint) -> tuple[Mint, Mint, OrderSide]:
    """(cotação, token, lado) na convenção do `Order`."""
    if side == IntentSide.SELL:
        return receive, spend, OrderSide.SELL
    return spend, receive, OrderSide.BUY


def _estimated_out(record: IntentRecord, spend: Mint, receive: Mint) -> int:
    """O recebido, estimado pela intenção (só vale sem a transação real)."""
    intent = record.intent
    if intent.side == IntentSide.BUY and intent.quantity:
        return receive.ui_to_raw(intent.quantity)
    if receive.is_usd_stable and intent.notional_usd:
        return receive.ui_to_raw(intent.notional_usd)
    return 0
