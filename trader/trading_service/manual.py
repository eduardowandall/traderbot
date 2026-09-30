"""Swaps manuais: qualquer par, sem posição, no mesmo caminho dos buckets.

A intenção passa por `execute_trade` (gateway, depois custos) e o fill vira
um `Order` gravado com `gateway.record_fill`, então swaps manuais aparecem em
`pnl` e `ledger list` com os valores efetivos e os custos pagos.

No `Order` de um swap manual, `input_mint` é o token gasto e `output_mint`
o recebido (`side=BUY`: "gasta o input"); `quantity` é o recebido e
`quote_amount` o gasto.

Valores em USD: quando um dos lados é stablecoin, vêm do próprio trade; senão
da Price API (`usd`, um retrato tirado antes do trade). Sem nenhum dos dois,
ficam desconhecidos e `price` fica em unidades do token gasto.
"""

import logging
from datetime import datetime
from decimal import Decimal

from trader.execution import TradeGateway
from trader.execution.fills import Fill, execute_trade
from trader.execution.orders import order_from_fill, priced_mints
from trader.market.prices import PriceOracle, usd_snapshot
from trader.models import SOLANA_MINTS, Mint, Order, OrderSide
from trader.models.intent import IntentSide, TradeIntent, with_idempotency_key
from trader.models.order import order_to_json
from trader.providers.jupiter.async_jupiter_svc import AsyncJupiterProvider
from trader.trading_service.protocol import SwapRequest

logger = logging.getLogger(__name__)


async def execute_swap(
    gateway: TradeGateway,
    provider: AsyncJupiterProvider,
    account: str,
    request: SwapRequest,
    source: str,
    now: datetime,
    prices: PriceOracle | None = None,
) -> Order:
    spend = SOLANA_MINTS[request.spend_mint]
    receive = SOLANA_MINTS[request.receive_mint]
    if spend.mint == receive.mint:
        raise ValueError("swap precisa de dois tokens diferentes")
    if request.amount <= 0:
        raise ValueError("a quantidade do swap deve ser maior que zero")
    # antes do trade: depois de EXECUTED nada pode falhar
    usd = await usd_snapshot(prices, priced_mints(spend, receive))
    intent = _intent(account, request, source, spend, usd)
    raw_amount = spend.ui_to_raw(request.amount)
    fill = await execute_trade(
        gateway,
        provider,
        intent,
        lambda: provider.swap_with_details(
            spend.mint, receive.mint, raw_amount, request.slippage_bps
        ),
    )
    order = swap_order(fill, spend, receive, now, usd)
    try:
        gateway.record_fill(intent.intent_id, order)
    except Exception as ex:
        # o swap aconteceu: falhar aqui faria o dono repetir e trocar duas vezes
        logger.error(
            f"Swap executado mas não gravado ({intent.intent_id}): {ex}; "
            f"ordem: {order_to_json(order)}"
        )
    return order


def _intent(
    account: str,
    request: SwapRequest,
    source: str,
    spend: Mint,
    usd: dict[str, Decimal],
) -> TradeIntent:
    rate = Decimal("1") if spend.is_usd_stable else usd.get(spend.mint)
    intent = TradeIntent(
        source=source,
        account=account,
        side=IntentSide.SWAP,
        spend_mint=spend.mint,
        receive_mint=request.receive_mint,
        spend_amount=request.amount,
        notional_usd=None if rate is None else request.amount * rate,
        rationale=request.rationale,
    )
    return with_idempotency_key(intent, request.idempotency_key)


def swap_order(
    fill: Fill,
    spend: Mint,
    receive: Mint,
    now: datetime,
    usd: dict[str, Decimal] | None = None,
) -> Order:
    """O fill de um swap manual como `Order`: compra de `receive` com `spend`."""
    return order_from_fill(fill, spend, receive, OrderSide.BUY, now, usd=usd)
