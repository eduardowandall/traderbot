"""B9: o custo por ida e volta (movimento sem custo - PnL líquido), do ledger."""

from datetime import UTC, datetime
from decimal import Decimal

from factories import memory_gateway

from trader.backtest import Tick
from trader.backtest.replay import ReplayQuoteClient
from trader.execution.trade.ledger.reports import _sell_cost
from trader.execution.trade.trading_service.service import TradeService
from trader.execution.trade.venues.paper import SimulatedWallet, paper_provider
from trader.execution.trade.venues.spot import SpotVenue
from trader.shared.models import SOLANA_MINTS, OrderSide
from trader.shared.models.costs import RoundTripCosts
from trader.shared.trading_service.protocol import OrderRequest

USDC = SOLANA_MINTS.get_by_symbol("USDC")
SOL = SOLANA_MINTS.get_by_symbol("SOL")
ENTRY = {"price": "100", "out_amount": 1000, "spend_amount": "10", "notional_usd": "10"}


def _sell(**fields):
    row = {
        "price": "110",
        "in_amount": 1000,
        "net_pnl_quote": "0.9",
        "quote_usd_price": None,
        "realized_pnl_usd": "0.9",
    }
    return row | fields


def test_cost_is_the_costless_move_minus_the_net_pnl():
    # gastou 10 a 100, saiu a 110: sem custo, +1; recebeu +0.9 -> custou 0.1
    assert _sell_cost(ENTRY, _sell()) == (Decimal("0.1"), Decimal("10"))


def test_a_partial_sell_counts_its_share_of_the_entry():
    cost, spend = _sell_cost(ENTRY, _sell(in_amount=250, net_pnl_quote="0.2")) or (
        None,
        None,
    )
    assert (cost, spend) == (Decimal("0.05"), Decimal("2.5"))


def test_a_non_stable_quote_converts_with_the_sell_quote_price():
    # par JUP-SOL: gasto e PnL em SOL, a 150 USD por SOL na venda
    leg = _sell_cost(ENTRY, _sell(quote_usd_price="150"))
    assert leg == (Decimal("15"), Decimal("1500"))


def test_without_native_pnl_it_falls_back_to_usd():
    leg = _sell_cost(ENTRY, _sell(net_pnl_quote=None, realized_pnl_usd="0.8"))
    assert leg == (Decimal("0.2"), Decimal("10"))


def test_without_tick_prices_there_is_nothing_to_measure():
    assert _sell_cost(ENTRY | {"price": None}, _sell()) is None


def test_the_summary_in_usd_and_bps():
    costs = RoundTripCosts()
    assert costs.per_trip_usd is None and "sem idas e voltas" in costs.describe()
    costs.add(Decimal("0.1"), Decimal("10"), closes=True)
    costs.add(Decimal("0.3"), Decimal("10"), closes=True)
    assert costs.per_trip_usd == Decimal("0.2") and costs.bps == Decimal("200")
    assert costs.describe() == "custo por ida e volta ~$0.2000 (200.0 bps) em 2"


async def _round_trip(service, quotes, name, buy_at, sell_at):
    quotes.tick = Tick(datetime.now(UTC), Decimal(buy_at))
    buy = OrderRequest(OrderSide.BUY, Decimal("0.2"), Decimal(buy_at))
    assert (await service.submit_order(name, buy)).filled
    quotes.tick = Tick(datetime.now(UTC), Decimal(sell_at))
    assert (await service.close_bucket(name, Decimal(sell_at))).filled


async def test_through_paper_and_the_ledger_in_a_window():
    # 50 bps por perna e nada mais: cada ida e volta custa ~100 bps
    quotes = ReplayQuoteClient(USDC, Decimal(50))
    provider = paper_provider(
        SimulatedWallet(initial={"USDC": Decimal(100), "SOL": Decimal(1)}),
        jupiter_client=quotes,
        fee_lamports=0,
        account_rent_lamports=0,
        slippage_bps=0,
    )
    service = TradeService(SpotVenue(provider), memory_gateway(), mode="paper")
    quotes.tick = Tick(datetime.now(UTC), Decimal(100))
    await service.open_bucket("b", USDC.mint, SOL.mint)

    # 20 USDC -> 0.199 SOL -> 19.8005 USDC: 0.1995 (99.75 bps)
    await _round_trip(service, quotes, "b", "100", "100")
    middle = datetime.now(UTC)
    # subiu 10%: sem custo +2; recebeu 0.199 x 110 x 0.995 - 20 = +1.78055
    await _round_trip(service, quotes, "b", "100", "110")

    ledger = service.gateway.ledger
    every = ledger.round_trip_costs("paper:b")
    assert (every.count, every.cost_usd) == (2, Decimal("0.41895"))
    later = ledger.round_trip_costs("paper:b", start=middle)
    assert (later.count, later.cost_usd) == (1, Decimal("0.21945"))
    assert later.notional_usd == Decimal(20)
