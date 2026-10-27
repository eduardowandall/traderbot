"""A25: o rent da conta de uma posição perp fica fora do custo por ida e volta.

A Jupiter nunca fecha a conta da posição e a reusa: é um depósito único, que
o bucket que a criou paga (o PnL o desconta, como no real), mas que não é um
custo de cada ida e volta. `round_trip_costs` o tira do custo e o mostra à
parte, em `account_rent_usd`.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from factories import PerpOracle, memory_gateway

from trader.execution.models.perp import PerpTerms, terms_to_json
from trader.execution.trade.ledger.reports import _add_leg
from trader.execution.trade.trading_service.service import TradeService
from trader.execution.trade.venues.paper import SimulatedWallet, paper_provider
from trader.execution.trade.venues.paper.perps import SimulatedPerpsVenue
from trader.execution.trade.venues.spot import SpotVenue
from trader.shared.models import SOLANA_MINTS, OrderSide
from trader.shared.models.costs import RoundTripCosts
from trader.shared.models.direction import Direction
from trader.shared.models.mints import SOL_MINT
from trader.shared.trading_service.protocol import OrderRequest

USDC = SOLANA_MINTS.get_by_symbol("USDC").mint
LONG2 = PerpTerms(SOL_MINT, Direction.LONG, Decimal(2))
RENT = 1_747_520  # a conta da posição (medida no real, 2026-10-12)
T0 = datetime(2026, 10, 12, 17, tzinfo=UTC)


def _entry(rent: int) -> dict:
    """A abertura do run 2 (A11b): 10 USDC de colateral, 2x comprado."""
    return {
        "price": "100",
        "out_amount": 200_000_000,  # 0.2 SOL de tamanho
        "spend_amount": "10",
        "notional_usd": "20",
        "perp_json": terms_to_json(LONG2),
        "rent_lamports": rent,
        "sol_usd_price": "108",
    }


def _close(net: Decimal) -> dict:
    return {
        "price": "100",
        "in_amount": 200_000_000,
        "net_pnl_quote": str(net),
        "quote_usd_price": "1",
        "realized_pnl_usd": str(net),
        "rent_lamports": 0,
        "sol_usd_price": "108",
        "closes_position": 1,
    }


def test_the_run_2_legs_cost_20_bps_with_the_rent_apart():
    # três idas e voltas a preço parado, 0.04 USD de custo cada; a primeira
    # abertura criou a conta da posição e o PnL dela desconta o rent
    rent_usd = Decimal(RENT) / 10**9 * 108
    totals = RoundTripCosts()
    for rent in (RENT, 0, 0):
        net = -Decimal("0.04") - (rent_usd if rent else 0)
        _add_leg(totals, _entry(rent), _close(net))

    assert totals.count == 3 and totals.notional_usd == 60
    assert totals.cost_usd == Decimal("0.12")
    assert totals.bps == Decimal(20)
    assert totals.account_rent_usd == rent_usd
    assert totals.describe() == (
        "custo por ida e volta ~$0.0400 (20.0 bps) em 3; "
        "rent da conta da perp ~$0.1887 à parte"
    )
    assert totals.as_dict()["account_rent_usd"] == rent_usd


def test_a_partial_close_takes_its_share_of_the_rent():
    rent_usd = Decimal(RENT) / 10**9 * 108
    totals = RoundTripCosts()
    half = _close(-Decimal("0.02") - rent_usd / 2) | {"in_amount": 100_000_000}
    _add_leg(totals, _entry(RENT), half)

    assert totals.cost_usd == Decimal("0.02")
    assert totals.account_rent_usd == rent_usd / 2


def test_a_spot_leg_keeps_its_token_rent_in_the_cost():
    # A15: o rent da conta de um token volta no encerramento (`rent_refund`)
    spot = _entry(RENT) | {"perp_json": None, "spend_amount": "20"}
    totals = RoundTripCosts()
    _add_leg(totals, spot, _close(-Decimal("0.2")))

    assert totals.cost_usd == Decimal("0.2") and totals.account_rent_usd == 0


async def test_through_paper_the_second_trip_costs_the_same_as_the_first():
    clock = [T0]
    oracle = PerpOracle("100")
    wallet = SimulatedWallet(initial={"USDC": Decimal(100), "SOL": Decimal(1)})
    perps = SimulatedPerpsVenue(
        wallet, oracle, clock=lambda: clock[0], borrow_bps_hour=Decimal(0)
    )
    service = TradeService(
        SpotVenue(paper_provider(wallet, jupiter_client=object())),
        memory_gateway(),
        mode="paper",
        clock=lambda: clock[0],
        prices=oracle,
        perps=perps,
    )
    await service.open_bucket(
        "long", USDC, SOL_MINT, budget_usd=Decimal(30), perp=LONG2
    )

    middle = None
    for _ in range(2):
        buy = OrderRequest(OrderSide.BUY, Decimal("0.1"), Decimal(100), "teste")
        assert (await service.submit_order("long", buy)).filled
        clock[0] += timedelta(minutes=1)
        sell = OrderRequest(OrderSide.SELL, Decimal(1000), Decimal(100), "teste")
        assert (await service.submit_order("long", sell)).filled
        # o ledger grava as intenções na hora de verdade
        middle = middle or datetime.now(UTC)

    ledger = service.gateway.ledger
    every = ledger.round_trip_costs("paper:long")
    second = ledger.round_trip_costs("paper:long", start=middle)
    assert every.count == 2 and second.count == 1
    assert every.cost_usd == 2 * second.cost_usd  # o rent não pesa na primeira
    assert every.account_rent_usd == Decimal(RENT) / 10**9 * 100  # SOL a 100
    assert second.account_rent_usd == 0
    # o bucket que criou a conta continua pagando o rent (A11c)
    assert ledger.pnl_totals("paper:long").rent_lamports == RENT
