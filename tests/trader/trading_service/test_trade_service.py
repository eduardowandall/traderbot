import random
from datetime import datetime
from decimal import Decimal

import pytest
from factories import mock_provider, open_ledger

from trader.backtest import Tick
from trader.backtest.replay import ReplayQuoteClient
from trader.execution import KillSwitch, TradeGateway
from trader.models import SOLANA_MINTS, OrderSide
from trader.models.intent import IntentSide
from trader.paper import SimulatedWallet, paper_provider
from trader.policy import Policy
from trader.trading_service.local import LocalTradeClient
from trader.trading_service.protocol import OrderRequest, ReplyStatus
from trader.trading_service.service import TradeService, remaining_budget

USDC = SOLANA_MINTS.get_by_symbol("USDC")
JUP = SOLANA_MINTS.get_by_symbol("JUP")
BONK = SOLANA_MINTS.get_by_symbol("BONK")
T0 = datetime(2026, 9, 1, 12, 0)
LOOSE = Policy(
    max_trade_usd=Decimal(1000),
    max_daily_notional_usd=Decimal(100000),
    max_trades_per_hour=100000,
    max_daily_loss_usd=Decimal(100000),
)


class Market:
    """Quotes sintéticas no preço atual (mesma cotação para qualquer token)."""

    def __init__(self, price="100"):
        self.client = ReplayQuoteClient(USDC, Decimal(0))
        self.set(price)

    def set(self, price):
        self.client.tick = Tick(T0, Decimal(str(price)))

    @property
    def price(self) -> Decimal:
        assert self.client.tick is not None
        return self.client.tick.price


def _service(tmp_path, wallet, market, policy=LOOSE, ledger=None):
    provider = paper_provider(wallet, jupiter_client=market.client)
    gateway = TradeGateway(
        ledger or open_ledger(), policy, KillSwitch(tmp_path / "HALT"), False
    )
    return TradeService(provider, gateway, mode="paper")


def _wallet(usdc="100"):
    return SimulatedWallet(initial={"USDC": Decimal(usdc), "SOL": Decimal("1")})


async def _open(service, name, token=JUP, budget=None):
    await service.open_bucket(name, USDC.mint, token.mint, budget)


def _buy(market, quantity="1", key=None):
    return OrderRequest(
        OrderSide.BUY, Decimal(quantity), market.price, "teste", idempotency_key=key
    )


def _sell(market, quantity):
    return OrderRequest(OrderSide.SELL, quantity, market.price, "teste")


def test_remaining_budget_only_shrinks():
    assert remaining_budget(Decimal(30), Decimal(-4)) == Decimal(26)
    assert remaining_budget(Decimal(30), Decimal(10)) == Decimal(30)
    assert remaining_budget(Decimal(30), Decimal(-40)) == Decimal(0)


class TestBudget:
    async def test_buy_is_capped_by_the_bucket_budget(self, tmp_path):
        market = Market("100")
        service = _service(tmp_path, _wallet("100"), market)
        await _open(service, "a", budget=Decimal(30))

        assert (await service.get_bucket("a")).available_usd == Decimal(30)
        reply = await service.submit_order("a", _buy(market, "1"))  # pediu 100 USD
        assert reply.filled and reply.order is not None
        assert reply.order.quantity == Decimal("0.3")  # 30 USD a 100
        assert reply.order.quote_amount == Decimal(30)

    async def test_realized_loss_shrinks_the_budget(self, tmp_path):
        market = Market("100")
        service = _service(tmp_path, _wallet("100"), market)
        await _open(service, "a", budget=Decimal(30))
        buy = await service.submit_order("a", _buy(market))
        assert buy.order is not None

        market.set("90")
        sell = await service.submit_order("a", _sell(market, buy.order.quantity))
        assert sell.filled
        snapshot = await service.get_bucket("a")
        assert snapshot.realized_usd == Decimal(-3)
        assert snapshot.available_usd == Decimal(27)

    async def test_bucket_without_budget_uses_the_wallet(self, tmp_path):
        market = Market("100")
        service = _service(tmp_path, _wallet("100"), market)
        await _open(service, "a")
        assert (await service.get_bucket("a")).available_usd == Decimal(100)

    async def test_buckets_sharing_a_wallet_never_overspend(self, tmp_path):
        # dois buckets de 40 numa carteira de 50: nenhuma compra pode passar
        # do saldo real nem do orçamento do bucket
        rng = random.Random(7)
        market = Market("100")
        wallet = _wallet("50")
        service = _service(tmp_path, wallet, market)
        budgets = {"jup": Decimal(40), "bonk": Decimal(40)}
        await _open(service, "jup", JUP, budgets["jup"])
        await _open(service, "bonk", BONK, budgets["bonk"])

        for _ in range(300):
            market.set(rng.randint(80, 120))
            name = rng.choice(list(budgets))
            snapshot = await service.get_bucket(name)
            if snapshot.position is None:
                cash_before = wallet.balance(USDC.mint)
                reply = await service.submit_order(name, _buy(market, "10"))
                assert reply.status in (ReplyStatus.FILLED, ReplyStatus.REJECTED)
                assert "insuficiente" not in " ".join(reply.reasons)
                if reply.order is not None:
                    spent = reply.order.quote_amount or Decimal(0)
                    assert spent <= min(cash_before, snapshot.available_usd)
            elif rng.random() < 0.5:
                qty = snapshot.position.entry_order.quantity
                assert (await service.submit_order(name, _sell(market, qty))).filled
            assert wallet.balance(USDC.mint) >= 0


class TestLifecycle:
    async def test_buckets_restore_independently(self, tmp_path):
        market = Market("100")
        wallet = _wallet("100")
        ledger = open_ledger()
        first = _service(tmp_path, wallet, market, ledger=ledger)
        await _open(first, "jup", JUP)
        await _open(first, "bonk", BONK)
        assert (await first.submit_order("jup", _buy(market, "0.1"))).filled

        # reinício: outro serviço sobre o mesmo ledger e a mesma carteira
        second = _service(tmp_path, wallet, market, ledger=ledger)
        await _open(second, "jup", JUP)
        await _open(second, "bonk", BONK)
        assert (await second.get_bucket("jup")).position is not None
        assert (await second.get_bucket("bonk")).position is None

    async def test_close_bucket_sells_exactly_once(self, tmp_path):
        market = Market("100")
        service = _service(tmp_path, _wallet("100"), market)
        await _open(service, "a", budget=Decimal(20))
        await service.submit_order("a", _buy(market))

        first = await service.close_bucket("a", market.price)
        assert first is not None and first.filled
        assert await service.close_bucket("a", market.price) is None

        assert service.gateway is not None
        sells = [
            r
            for r in service.gateway.ledger.list_intents(50)
            if r.intent.side == IntentSide.SELL and r.intent.account == "paper:a"
        ]
        assert len(sells) == 1

    async def test_rationale_and_source_reach_the_ledger(self, tmp_path):
        market = Market("100")
        service = _service(tmp_path, _wallet("100"), market)
        await service.open_bucket("a", USDC.mint, JUP.mint, source="spec:abc")
        await service.submit_order("a", _buy(market, "0.1"))
        assert service.gateway is not None
        [record] = service.gateway.ledger.list_intents(1)
        assert record.intent.rationale == "teste"
        assert record.intent.source == "spec:abc"
        assert record.intent.account == "paper:a"

    async def test_open_twice_and_unknown_bucket_are_errors(self, tmp_path):
        service = _service(tmp_path, _wallet(), Market())
        await _open(service, "a")
        with pytest.raises(ValueError, match="já está aberto"):
            await _open(service, "a")
        with pytest.raises(ValueError, match="não está aberto"):
            await service.get_bucket("nope")

    async def test_open_reads_no_balances_and_reconciles_only_when_tracked(self):
        for tracks in (True, False):
            provider = mock_provider(balances_track_fills=tracks)
            service = TradeService(provider, gateway=None)
            await service.open_bucket("a", USDC.mint, JUP.mint)
            provider.get_account_balance.assert_not_awaited()


class TestReplies:
    async def test_policy_denial(self, tmp_path):
        market = Market("100")
        tight = Policy(max_trade_usd=Decimal(5))
        service = _service(tmp_path, _wallet(), market, policy=tight)
        await _open(service, "a", budget=Decimal(20))
        reply = await service.submit_order("a", _buy(market))
        assert reply.status == ReplyStatus.DENIED
        assert "acima do limite" in reply.reasons[0]

    async def test_duplicate_key_is_denied(self, tmp_path):
        market = Market("100")
        service = _service(tmp_path, _wallet(), market)
        await _open(service, "a", budget=Decimal(10))
        assert (await service.submit_order("a", _buy(market, key="k"))).filled
        await service.close_bucket("a", market.price)
        again = await service.submit_order("a", _buy(market, key="k"))
        assert again.status == ReplyStatus.DENIED

    async def test_nothing_to_sell_is_rejected(self, tmp_path):
        market = Market("100")
        service = _service(tmp_path, _wallet(), market)
        await _open(service, "a")
        reply = await service.submit_order("a", _sell(market, Decimal(1)))
        assert reply.status == ReplyStatus.REJECTED
        assert "Sem posicão" in reply.reasons[0]

    async def test_unexpected_failure_is_an_error_reply(self, tmp_path, mock_sleep):
        market = Market("100")
        service = _service(tmp_path, _wallet(), market)
        await _open(service, "a", budget=Decimal(10))

        async def no_route(*args, **kwargs):
            raise ConnectionError("jupiter fora do ar")

        market.client.get_quote = no_route  # type: ignore[method-assign]
        reply = await service.submit_order("a", _buy(market))
        assert reply.status == ReplyStatus.ERROR
        assert reply.error is not None and "jupiter fora do ar" in reply.error


async def test_local_client_is_bound_to_one_bucket(tmp_path):
    market = Market("100")
    service = _service(tmp_path, _wallet(), market)
    client = LocalTradeClient(
        service, "a", USDC.mint, JUP.mint, Decimal(10), owns_service=True
    )
    await client.open()
    assert (await client.bucket()).budget_usd == Decimal(10)
    assert (await client.submit(_buy(market))).filled
    await client.aclose()
