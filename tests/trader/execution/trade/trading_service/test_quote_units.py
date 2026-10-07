"""Buckets de pares sem stablecoin (B6): orçamento em USD, saldo em cotação."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from factories import open_ledger, trade_runner

from trader.backtest import Tick
from trader.backtest.replay import ReplayQuoteClient
from trader.execution.market import JupiterPriceOracle
from trader.execution.market.jupiter.async_jupiter_client import AsyncJupiterClient
from trader.execution.trade.gateway import TradeGateway
from trader.execution.trade.policy import Policy
from trader.execution.trade.trading_service.service import TradeService
from trader.execution.trade.venues.paper import SimulatedWallet, paper_provider
from trader.execution.trade.venues.spot import SpotVenue
from trader.shared.models import SOLANA_MINTS, OrderSide
from trader.shared.trading_service.protocol import OrderRequest, ReplyStatus

SOL = SOLANA_MINTS.get_by_symbol("SOL")
JUP = SOLANA_MINTS.get_by_symbol("JUP")
USDC = SOLANA_MINTS.get_by_symbol("USDC")
T0 = datetime(2026, 10, 1, tzinfo=UTC)
LOOSE = Policy(
    max_trade_usd=Decimal(1000),
    max_daily_notional_usd=Decimal(100000),
    max_trades_per_hour=100000,
    max_daily_loss_usd=Decimal(100000),
)


def _oracle(prices: dict | None):
    client = AsyncMock(spec=AsyncJupiterClient)
    if prices is None:
        client.get_usd_prices = AsyncMock(side_effect=OSError("Price API fora"))
    else:
        client.get_usd_prices = AsyncMock(return_value=prices)
    return JupiterPriceOracle(client)


def _service(prices) -> TradeService:
    quotes = ReplayQuoteClient(SOL, Decimal(0))
    quotes.tick = Tick(T0, Decimal("0.005"), Decimal(200))  # SOL por JUP
    wallet = SimulatedWallet(initial={"SOL": Decimal(2), "USDC": Decimal(10)})
    gateway = TradeGateway(open_ledger(), LOOSE, False)
    return TradeService(
        SpotVenue(paper_provider(wallet, jupiter_client=quotes)),
        gateway,
        mode="paper",
        clock=lambda: T0,
        prices=_oracle(prices),
    )


async def test_the_usd_budget_becomes_a_cap_in_the_quote_token():
    service = _service({SOL.mint: Decimal(200)})
    await service.open_bucket("jup", SOL.mint, JUP.mint, budget_usd=Decimal(50))

    snapshot = await service.get_bucket("jup")

    assert snapshot.quote_usd == Decimal(200)
    assert snapshot.available == Decimal("0.25")  # 50 USD a 200 USD/SOL
    # um pedido acima do teto é ajustado a ele
    reply = await service.submit_order(
        "jup", OrderRequest(OrderSide.BUY, Decimal(1000), Decimal("0.005"))
    )
    assert reply.status == ReplyStatus.FILLED and reply.order is not None
    assert reply.order.quote_amount == Decimal("0.25")
    assert reply.order.price == Decimal(1)  # USD por JUP


async def test_a_sell_notional_is_converted_to_usd():
    service = _service({SOL.mint: Decimal(200)})
    await service.open_bucket("jup", SOL.mint, JUP.mint, budget_usd=Decimal(50))
    await service.submit_order(
        "jup", OrderRequest(OrderSide.BUY, Decimal(20), Decimal("0.005"))
    )
    await service.submit_order(
        "jup", OrderRequest(OrderSide.SELL, Decimal(20), Decimal("0.005"))
    )
    sell = service.gateway.ledger.list_intents(1)[0]
    assert sell.intent.notional_usd == Decimal(20)  # 20 JUP x 0.005 SOL x 200


async def test_without_the_quote_price_a_budget_cannot_be_checked():
    service = _service(None)
    with pytest.raises(ValueError, match="sem preço USD"):
        await service.open_bucket("jup", SOL.mint, JUP.mint, budget_usd=Decimal(50))

    # sem orçamento abre, mas o snapshot mostra o preço desconhecido
    await service.open_bucket("free", SOL.mint, JUP.mint)
    snapshot = await service.get_bucket("free")
    assert snapshot.quote_usd is None and snapshot.available == Decimal(2) - Decimal(
        "0.02"
    )  # sem teto: a carteira menos a reserva de SOL


async def test_a_budgeted_bucket_offers_nothing_while_the_price_is_unknown():
    service = _service({SOL.mint: Decimal(200)})
    await service.open_bucket("jup", SOL.mint, JUP.mint, budget_usd=Decimal(50))
    service.prices = _oracle(None)

    snapshot = await service.get_bucket("jup")

    assert (snapshot.available, snapshot.quote_usd) == (Decimal(0), None)


async def test_the_exit_sweep_prices_leftovers_in_the_quote_token():
    prices = {JUP.mint: Decimal(1), SOL.mint: Decimal(200)}
    runner = trade_runner(_service(prices), prices=prices)
    assert await runner._pair_price(JUP.mint, SOL.mint) == Decimal("0.005")
    assert await runner._pair_price(JUP.mint, USDC.mint) == Decimal(1)
    # sem o preço da cotação, nada a vender
    priceless = trade_runner(_service(prices), prices={JUP.mint: Decimal(1)})
    assert await priceless._pair_price(JUP.mint, SOL.mint) is None
