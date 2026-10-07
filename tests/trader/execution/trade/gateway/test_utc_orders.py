"""Ordens com horário em UTC (B7): como o resto do ledger."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from factories import memory_gateway

from trader.backtest import Tick
from trader.backtest.replay import ReplayQuoteClient
from trader.execution.trade.trading_service.service import TradeService
from trader.execution.trade.venues.paper import SimulatedWallet, paper_provider
from trader.execution.trade.venues.spot import SpotVenue
from trader.shared.models import SOLANA_MINTS, OrderSide
from trader.shared.trading_service.protocol import OrderRequest

USDC = SOLANA_MINTS.get_by_symbol("USDC")
SOL = SOLANA_MINTS.get_by_symbol("SOL")


async def test_orders_are_stamped_in_utc_by_default():
    quotes = ReplayQuoteClient(USDC, Decimal(0))
    quotes.tick = Tick(datetime.now(UTC), Decimal(100))
    wallet = SimulatedWallet(initial={"USDC": Decimal(100), "SOL": Decimal(1)})
    service = TradeService(
        SpotVenue(paper_provider(wallet, jupiter_client=quotes)), memory_gateway()
    )
    await service.open_bucket("sol", USDC.mint, SOL.mint)

    reply = await service.submit_order(
        "sol", OrderRequest(OrderSide.BUY, Decimal("0.1"), Decimal(100))
    )

    assert reply.order is not None
    stamp = reply.order.timestamp
    assert stamp.utcoffset() == timedelta(0)
    assert abs(datetime.now(UTC) - stamp) < timedelta(minutes=1)
