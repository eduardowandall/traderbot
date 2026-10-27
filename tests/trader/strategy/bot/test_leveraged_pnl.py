"""A31: as linhas de uma perp dizem o que a posição alavancada ganhou e pagou.

O movimento do preço é sobre o tamanho; a 150x, 0.5% de preço são dezenas de
% do colateral postado, e as taxas do venue pesam o mesmo tanto.
"""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from factories import make_order

from trader.shared.models.direction import Direction
from trader.shared.models.order import OrderSide
from trader.shared.models.perp import PerpFill
from trader.shared.models.position import Position
from trader.strategy.bot.loop import position_pnl, round_trip_line

# 10 USD postados a 150x: 1500 de tamanho, 0.9 de taxa de abertura (0.06%)
ENTRY = PerpFill(
    direction=Direction.LONG,
    leverage=Decimal(150),
    price=Decimal(100),
    size_usd=Decimal(1500),
    collateral_usd=Decimal("9.1"),
    fees_usd=Decimal("0.9"),
    borrow_bps_hour=Decimal("0.15"),
)


def _position() -> Position:
    order = replace(make_order(quantity="15"), quote_amount=Decimal(10), perp=ENTRY)
    return Position(entry_order=order)


def test_the_position_line_shows_the_return_on_collateral_at_150x():
    position = _position()
    now = position.entry_order.timestamp + timedelta(hours=1)
    price = Decimal("100.5")  # +0.5%

    assert position.unrealized_pnl_percent(price) == Decimal("0.5")
    # 7.5 de preço, menos a taxa de fechar agora (0.9), a da abertura (já
    # fora do colateral) e 1 h de empréstimo (1500 x 0.15 bps)
    assert position.unrealized_usd(price, now) == Decimal("5.6775")
    assert position.return_on_collateral(price, now) == Decimal("56.775")
    line = position_pnl(position, price, Decimal(1), now)
    assert "preço [green]0.50%" in line
    assert "colateral [green]56.78%" in line
    assert line.endswith("~$+5.6775")


def test_a_spot_position_keeps_the_price_move_only():
    position = Position(entry_order=make_order())
    now = position.entry_order.timestamp
    line = position_pnl(position, Decimal(110), Decimal(1), now)
    assert line == "[green]10.00%[/green] (~$+1.0000)"
    assert position.return_on_collateral(Decimal(110), now) is None


def test_the_sell_line_splits_the_round_trip():
    # o exemplo do run real: +0.0107 de preço, 0.052 de taxas, 0.0025 de
    # empréstimo, e só o bruto -0.0438 aparecia
    entry = replace(
        ENTRY,
        leverage=Decimal(2),
        size_usd=Decimal(20),
        collateral_usd=Decimal("9.974"),
        fees_usd=Decimal("0.026"),
    )
    held = Position(
        replace(make_order(quantity="0.2"), quote_amount=Decimal(10), perp=entry)
    )
    exit_perp = replace(
        entry,
        collateral_usd=Decimal("9.9562"),
        fees_usd=Decimal("0.026"),
        borrow_usd=Decimal("0.0025"),
    )
    sell = replace(
        make_order("sell", quantity="0.2"),
        quote_amount=Decimal("9.9562"),
        perp=exit_perp,
    )

    line = round_trip_line(held, sell)

    assert line == (
        "fechamento: bruto -0.043800 USDC (preço +0.010700, taxas perp "
        "-0.052000, empréstimo -0.002500), rede 0.000000000 SOL"
    )
    assert round_trip_line(held, replace(sell, side=OrderSide.BUY)) is None
    assert round_trip_line(None, sell) is None
