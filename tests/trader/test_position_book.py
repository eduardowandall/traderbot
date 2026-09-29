from datetime import datetime
from decimal import Decimal

import pytest

from trader.models import SOLANA_MINTS, Order, OrderSide
from trader.models.book import PositionBook
from trader.models.costs import TradeCosts

USDC = SOLANA_MINTS.get_by_symbol("USDC").mint
SOL = SOLANA_MINTS.get_by_symbol("SOL").mint
T0 = datetime(2026, 9, 1, 12, 0)


def _order(side, quantity, price, quote_amount=None, costs=None):
    return Order(
        order_id=f"{side}-{price}",
        input_mint=USDC,
        output_mint=SOL,
        quantity=Decimal(quantity),
        price=Decimal(price),
        side=side,
        timestamp=T0,
        quote_amount=None if quote_amount is None else Decimal(quote_amount),
        quote_usd=Decimal("1"),
        sol_usd=Decimal(price),
        sol_in_quote=Decimal(price),
        costs=costs,
    )


def _buy(**kwargs):
    return _order(OrderSide.BUY, "1", "100", quote_amount="100", **kwargs)


def _sell(**kwargs):
    return _order(OrderSide.SELL, "1", "110", quote_amount="110", **kwargs)


def test_round_trip_books_native_and_usd_pnl():
    fee = TradeCosts("onchain", fee_lamports=10_000_000)  # 0.01 SOL por perna
    book = PositionBook("USDC")

    book.open(_buy(costs=fee))
    closed = book.close(_sell(costs=fee))

    assert book.position is None
    assert closed.position.exit_order is not None
    assert closed.pnl is not None and closed.pnl.complete
    assert book.gross_quote == Decimal("10")
    assert book.costs_sol == Decimal("0.02")
    # custos: 0.01 SOL a 100 na entrada + 0.01 SOL a 110 na saída
    assert book.net_quote == Decimal("10") - Decimal("2.1")
    assert book.realized_usd == closed.realized_usd == Decimal("7.9")
    assert book.incomplete == 0


def test_orders_without_native_amounts_count_as_incomplete():
    book = PositionBook("USDC")
    book.open(_order(OrderSide.BUY, "1", "100"))

    closed = book.close(_order(OrderSide.SELL, "1", "110"))

    assert closed.pnl is None
    assert book.realized_usd == Decimal("10")  # preço x quantidade
    assert book.incomplete == 1
    assert "[!] 1 trade(s) incompleto(s)" in book.summary()


def test_one_position_at_a_time():
    book = PositionBook("USDC")
    with pytest.raises(ValueError, match="não há posição"):
        book.close(_sell())
    book.open(_buy())
    with pytest.raises(ValueError, match="já existe"):
        book.open(_buy())


def test_restored_reopens_the_entry_and_keeps_totals():
    entry = _buy()
    book = PositionBook.restored(
        "USDC",
        realized_usd=Decimal("-3"),
        gross_quote=Decimal("-2"),
        net_quote=Decimal("-3"),
        costs_sol=Decimal("0.01"),
        incomplete=1,
        entry=entry,
    )

    assert book.position is not None
    assert book.position.entry_order == entry
    assert book.summary() == (
        "PNL líquido -3.000000 USDC (~$-3.0000); bruto -2.000000, "
        "custos 0.010000000 SOL [!] 1 trade(s) incompleto(s)"
    )
