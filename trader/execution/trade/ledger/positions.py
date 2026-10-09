"""A posição aberta de uma conta, reconstruída das pernas do ledger.

A entrada é a última compra executada menos as vendas depois dela (e mais o
colateral que uma perp recebeu, A12), como o `PositionBook` faria em memória
(uma perna por vez). Sem a ordem gravada (o
`record_fill` falhou, ou a intenção foi resolvida à mão), a entrada sai da
própria linha: melhor uma posição com valores aproximados do que comprar de
novo ou nunca conseguir vender.
"""

import logging
from dataclasses import replace
from decimal import Decimal

from trader.execution.models.book import added_entry, rest_of
from trader.execution.models.intent import IntentRecord, IntentSide, TradeIntent
from trader.shared.models.order import Order, OrderSide, order_from_json
from trader.shared.models.perp import PerpFill
from trader.shared.models.position import Position

logger = logging.getLogger(__name__)


def open_entry(legs: list[IntentRecord]) -> Order | None:
    """A entrada da posição aberta (`legs`: `Ledger.legs_since_last_buy`)."""
    position = open_position(legs)
    return None if position is None else position.entry_order


def open_position(legs: list[IntentRecord]) -> Position | None:
    """A posição aberta com as contagens desde a entrada (A20): as vendas
    que pediram só uma parte e o colateral a mais."""
    if not legs or legs[0].intent.side != IntentSide.BUY:
        return None
    position: Position | None = Position(_order_of(legs[0]))
    for leg in legs[1:]:
        if position is None:
            return None
        if leg.intent.side == IntentSide.ADD:
            position = _after_add(position, leg)
        else:
            position = _after_sell(position, leg)
    return position


def _after_add(held: Position, add: IntentRecord) -> Position:
    """Colateral a mais (A12); sem a ordem gravada, o gasto da intenção."""
    entry = held.entry_order
    if add.order_json:
        entry = added_entry(entry, order_from_json(add.order_json))
    else:
        posted = (entry.quote_amount or Decimal(0)) + add.intent.spend_amount
        entry = replace(entry, quote_amount=posted)
    return replace(held, entry_order=entry, top_ups=held.top_ups + 1)


def _after_sell(held: Position, sell: IntentRecord) -> Position | None:
    """O que sobra depois da venda."""
    requested = sell.intent.quantity
    if sell.order_json:
        order = order_from_json(sell.order_json)
        if order.closes_position:
            return None
        return rest_of(held, order.quantity, requested)
    # `record_fill` falhou: vale o que a intenção sabia antes de executar
    if sell.intent.closes_position:
        return None
    return rest_of(held, requested or sell.intent.spend_amount, requested)


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
