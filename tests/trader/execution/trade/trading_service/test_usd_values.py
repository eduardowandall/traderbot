"""Valores em USD vindos da Price API: notional de pares sem stablecoin e
custos em SOL de pares sem SOL (item A4 do plano)."""

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock

from factories import open_ledger

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
from trader.shared.models.costs import TradeRates, with_sol_usd
from trader.shared.trading_service.protocol import OrderRequest, ReplyStatus

USDC = SOLANA_MINTS.get_by_symbol("USDC")
SOL = SOLANA_MINTS.get_by_symbol("SOL")
JUP = SOLANA_MINTS.get_by_symbol("JUP")
BONK = SOLANA_MINTS.get_by_symbol("BONK")
T0 = datetime(2026, 9, 1, 12, 0)
LOOSE = Policy(
    max_trade_usd=Decimal(1000),
    max_daily_notional_usd=Decimal(100000),
    max_trades_per_hour=100000,
    max_daily_loss_usd=Decimal(100000),
)


def _oracle(prices):
    client = AsyncMock(spec=AsyncJupiterClient)
    client.get_usd_prices = AsyncMock(return_value=prices)
    return JupiterPriceOracle(client)


def _service(tmp_path, ledger, prices=None, policy=LOOSE, quote=USDC, price="100"):
    client = ReplayQuoteClient(quote, Decimal(0))
    client.tick = Tick(T0, Decimal(price))
    wallet = SimulatedWallet(
        initial={"USDC": Decimal("100"), "SOL": Decimal("2"), "JUP": Decimal("50")}
    )
    gateway = TradeGateway(ledger, policy, False)
    provider = paper_provider(wallet, jupiter_client=client)
    return TradeService(
        SpotVenue(provider), gateway, mode="paper", clock=lambda: T0, prices=prices
    )


def _order(side, quantity, price="100"):
    return OrderRequest(side, Decimal(quantity), Decimal(price), "teste")


def test_with_sol_usd_only_fills_what_is_missing():
    known = TradeRates(Decimal("1"), Decimal("150"), Decimal("150"))
    assert with_sol_usd(known, Decimal("999")) is known
    missing = TradeRates(Decimal("0.5"), None, None)
    assert with_sol_usd(missing, None) is missing
    assert with_sol_usd(missing, Decimal("150")) == TradeRates(
        Decimal("0.5"), Decimal("150"), Decimal("300")
    )


class TestBucketCosts:
    async def _round_trip(self, tmp_path, prices):
        service = _service(tmp_path, open_ledger(), prices)
        await service.open_bucket("jup", USDC.mint, JUP.mint)
        bought = await service.submit_order("jup", _order(OrderSide.BUY, "0.2"))
        assert bought.order is not None
        sold = await service.submit_order(
            "jup", _order(OrderSide.SELL, str(bought.order.quantity))
        )
        assert sold.order is not None, sold
        return service._bucket("jup").account.book, sold.order

    async def test_a_pair_without_sol_prices_its_costs_with_the_api(self, tmp_path):
        book, sell = await self._round_trip(tmp_path, _oracle({SOL.mint: Decimal(150)}))

        assert sell.sol_usd == Decimal(150)
        assert book.incomplete == 0
        assert book.costs_sol > 0  # as taxas de rede simuladas contaram
        assert "[!]" not in book.summary()

    async def test_without_prices_the_costs_stay_incomplete(self, tmp_path):
        book, sell = await self._round_trip(tmp_path, None)

        assert sell.sol_usd is None
        assert book.incomplete == 1


class TestBucketNotional:
    async def test_a_non_stable_input_gets_its_usd_value(self, tmp_path):
        ledger = open_ledger()
        service = _service(
            tmp_path, ledger, _oracle({SOL.mint: Decimal(150)}), quote=SOL, price="0.01"
        )
        await service.open_bucket("jup-sol", SOL.mint, JUP.mint)

        reply = await service.submit_order(
            "jup-sol", _order(OrderSide.BUY, "10", price="0.01")
        )

        assert reply.status == ReplyStatus.FILLED
        (record,) = ledger.list_intents()
        # 10 x 0.01 = 0.1 SOL gastos, a 150 USD
        assert record.intent.notional_usd == Decimal("15.00")

    async def test_no_price_means_unknown_value_and_a_denial(self, tmp_path):
        down = _oracle({})
        down.client.get_usd_prices.side_effect = OSError("down")
        service = _service(tmp_path, open_ledger(), down, policy=Policy(), quote=SOL)
        await service.open_bucket("jup-sol", SOL.mint, JUP.mint)

        reply = await service.submit_order(
            "jup-sol", _order(OrderSide.BUY, "10", price="0.01")
        )

        assert reply.status == ReplyStatus.DENIED
        assert "desconhecido" in reply.reasons[0]
