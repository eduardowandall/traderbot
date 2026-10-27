"""A27: com uma posição aberta, a linha do bucket mostra os custos da entrada.

O resumo do bucket (`PositionBook.summary`) é o PnL realizado e só conta os
custos de uma ida e volta no fechamento: sem isto, o ticker lia `custos 0`
com uma perp aberta (G5 do A11b run 2).
"""

from dataclasses import replace
from decimal import Decimal

from factories import make_order

from trader.shared.models.costs import ONCHAIN, QUOTE, TradeCosts
from trader.shared.models.direction import Direction
from trader.shared.models.perp import PerpFill
from trader.shared.models.position import Position
from trader.shared.trading_service.protocol import BucketSnapshot
from trader.strategy.bot.loop import bucket_line, entry_costs, with_entry_costs

SUMMARY = "PNL líquido +0.000000 USDC (~$+0.0000); bruto +0.000000, custos 0 SOL"
PERP = PerpFill(
    direction=Direction.LONG,
    leverage=Decimal(2),
    price=Decimal(100),
    size_usd=Decimal(20),
    collateral_usd=Decimal("9.982"),
    fees_usd=Decimal("0.018"),
    borrow_bps_hour=Decimal("0.15"),
)
COSTS = TradeCosts(
    ONCHAIN,
    fee_lamports=105_000,
    priority_fee_lamports=100_000,
    rent_lamports=1_747_520,
)


def _snapshot(position: Position | None) -> BucketSnapshot:
    return BucketSnapshot(
        "b", Decimal(0), position, Decimal(0), Decimal(1), pnl_summary=SUMMARY
    )


def _perp_position(costs: TradeCosts | None = COSTS) -> Position:
    order = replace(make_order(quantity="0.2"), costs=costs, perp=PERP)
    return Position(entry_order=order)


def test_the_ticker_line_shows_the_open_perps_entry_costs():
    line = bucket_line(_snapshot(_perp_position()), Decimal(100))

    assert line.startswith(SUMMARY)
    assert "entrada: custos 0.001852520 SOL (rent 0.001747520)" in line
    assert "taxas perp $0.0180" in line
    assert "aberto ~$" in line and "long 2x" in line


def test_the_bar_log_carries_the_entry_costs_too():
    assert with_entry_costs(_snapshot(_perp_position())) == (
        f"{SUMMARY}; entrada: custos 0.001852520 SOL (rent 0.001747520), "
        "taxas perp $0.0180"
    )


def test_flat_bucket_keeps_the_realized_summary():
    assert with_entry_costs(_snapshot(None)) == SUMMARY
    assert bucket_line(_snapshot(None), Decimal(100)) == SUMMARY


def test_unknown_costs_leave_only_the_perp_fees():
    unknown = TradeCosts(QUOTE)
    assert entry_costs(_perp_position(unknown)) == "entrada: taxas perp $0.0180"


def test_a_spot_entry_shows_its_network_fee_and_rent():
    order = replace(make_order(), costs=COSTS)
    assert entry_costs(Position(entry_order=order)) == (
        "entrada: custos 0.001852520 SOL (rent 0.001747520)"
    )
