"""A posição aberta de uma conta, reconstruída das pernas do ledger.

A entrada é a última compra executada menos as vendas depois dela, como o
`PositionBook` faria em memória (uma venda por vez). Sem a ordem gravada (o
`record_fill` falhou, ou a intenção foi resolvida à mão), a entrada sai da
própria linha: melhor uma posição com valores aproximados do que comprar de
novo ou nunca conseguir vender.
"""

import logging
from decimal import Decimal

from trader.execution.models.book import remainder_entry
from trader.execution.models.intent import IntentRecord, IntentSide, TradeIntent
from trader.shared.models.order import Order, OrderSide, order_from_json
from trader.shared.models.perp import PerpFill

logger = logging.getLogger(__name__)


def open_entry(legs: list[IntentRecord]) -> Order | None:
    """A entrada da posição aberta (`legs`: `Ledger.legs_since_last_buy`)."""
    if not legs or legs[0].intent.side != IntentSide.BUY:
        return None
    entry: Order | None = _order_of(legs[0])
    for sell in legs[1:]:
        if entry is None:
            return None
        entry = _after_sell(entry, sell)
    return entry


def _after_sell(entry: Order, sell: IntentRecord) -> Order | None:
    """O que sobra depois da venda."""
    if sell.order_json:
        order = order_from_json(sell.order_json)
        return None if order.closes_position else remainder_entry(entry, order.quantity)
    # `record_fill` falhou: vale o que a intenção sabia antes de executar
    if sell.intent.closes_position:
        return None
    sold = sell.intent.quantity or sell.intent.spend_amount
    return remainder_entry(entry, sold)


def _order_of(record: IntentRecord) -> Order:
    if record.order_json:
        return order_from_json(record.order_json)
    return _entry_from_record(record)


def _entry_from_record(record: IntentRecord) -> Order:
    intent = record.intent
    quote, token = intent.pair()  # uma compra
    quantity = (
        token.raw_to_ui(record.out_amount) if record.out_amount else intent.quantity
    ) or Decimal("0")
    spent = (
        quote.raw_to_ui(record.in_amount) if record.in_amount else intent.spend_amount
    )
    fill_price = spent / quantity if quantity else Decimal("0")
    perp = _perp_of(intent, quantity)
    if perp is not None:
        fill_price = perp.price
    logger.warning(
        f"Entrada {intent.intent_id} sem ordem gravada: reconstruída da intenção"
    )
    return Order(
        order_id=record.signature or intent.intent_id,
        input_mint=quote.mint,
        output_mint=token.mint,
        quantity=quantity,
        price=intent.price if intent.price is not None else fill_price,
        side=OrderSide.BUY,
        timestamp=record.updated_at,
        requested_quantity=intent.quantity,
        requested_price=intent.price,
        fill_price=fill_price,
        quote_amount=spent,
        perp=perp,
    )


def _perp_of(intent: TradeIntent, quantity: Decimal) -> PerpFill | None:
    """Uma perp sem ordem gravada: o lado e o tamanho da intenção (taxas 0)."""
    terms = intent.perp
    if terms is None or intent.price is None:
        return None
    size = quantity * intent.price
    return PerpFill(
        direction=terms.direction,
        leverage=terms.leverage,
        price=intent.price,
        size_usd=size,
        collateral_usd=size / terms.leverage,
        fees_usd=Decimal(0),
        borrow_bps_hour=Decimal(0),
    )
