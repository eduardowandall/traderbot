import random
from datetime import datetime
from decimal import Decimal

import pytest
from factories import memory_gateway, mock_provider, open_ledger

from trader.backtest import Tick
from trader.backtest.replay import ReplayQuoteClient
from trader.execution.gateway import TradeGateway
from trader.execution.models.intent import IntentSide
from trader.execution.policy import Policy
from trader.execution.trading_service.local import LocalTradeClient
from trader.execution.trading_service.service import TradeService, remaining_budget
from trader.execution.venues.paper import SimulatedWallet, paper_provider
from trader.shared.models import SOLANA_MINTS, OrderSide
from trader.shared.trading_service.protocol import OrderRequest, ReplyStatus

USDC = SOLANA_MINTS.get_by_symbol("USDC")
JUP = SOLANA_MINTS.get_by_symbol("JUP")
BONK = SOLANA_MINTS.get_by_symbol("BONK")
T0 = datetime(2026, 9, 1, 12, 0)
LOOSE = Policy(
    max_trade_usd=Decimal(1000),
    max_daily_notional_usd=Decimal(100000),
    max_trades_per_hour=100000,
    max_trades_per_hour_per_bucket=100000,
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
    gateway = TradeGateway(ledger or open_ledger(), policy, False)
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

        assert (await service.get_bucket("a")).available == Decimal(30)
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
        assert snapshot.available == Decimal(27)

    async def test_bucket_without_budget_uses_the_wallet(self, tmp_path):
        market = Market("100")
        service = _service(tmp_path, _wallet("100"), market)
        await _open(service, "a")
        assert (await service.get_bucket("a")).available == Decimal(100)

    async def test_budgets_that_dont_fit_the_wallet_are_refused(self, tmp_path):
        # B2: dois buckets de 40 numa carteira de 50 não abrem juntos
        service = _service(tmp_path, _wallet("50"), Market("100"))
        await _open(service, "jup", JUP, Decimal(40))
        with pytest.raises(ValueError, match="passam do que a carteira tem"):
            await _open(service, "bonk", BONK, Decimal(40))

    async def test_a_position_counts_at_cost_in_the_allocation(self, tmp_path):
        market = Market("100")
        service = _service(tmp_path, _wallet("50"), market)
        await _open(service, "jup", JUP, Decimal(40))
        assert (await service.submit_order("jup", _buy(market, "0.3"))).filled
        # 20 livres + 30 na posição do jup: cabem 40 (jup) + 10 (bonk)
        await _open(service, "bonk", BONK, Decimal(10))

    async def test_one_balance_read_serves_every_bucket(self, tmp_path):
        market = Market("100")
        service = _service(tmp_path, _wallet(), market)
        await _open(service, "jup", JUP, Decimal(20))
        await _open(service, "bonk", BONK, Decimal(20))
        a, b = service._buckets["jup"].account, service._buckets["bonk"].account
        assert a.wallet is b.wallet is service.wallet
        # a compra de um invalida o cache de todos: o outro vê o saldo novo
        assert (await service.submit_order("jup", _buy(market, "0.1"))).filled
        assert (await service.get_bucket("bonk")).available == Decimal(20)
        assert await b.get_balance(USDC.pubkey) == Decimal(90)

    async def test_buckets_sharing_a_wallet_never_overspend(self, tmp_path):
        # dois buckets de 40 numa carteira de 80: nenhuma compra pode passar
        # do saldo real nem do orçamento do bucket
        rng = random.Random(7)
        market = Market("100")
        wallet = _wallet("80")
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
                    assert spent <= min(cash_before, snapshot.available)
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

    async def test_open_reads_no_balances_without_positions_or_budget(self):
        provider = mock_provider()
        service = TradeService(provider, memory_gateway())
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


class TestReconcile:
    async def _bought_elsewhere(self, tmp_path, ledger):
        """Um processo anterior comprou JUP e deixou a posição no ledger."""
        market = Market("100")
        first = _service(tmp_path, _wallet(), market, ledger=ledger)
        await _open(first, "old", JUP)
        assert (await first.submit_order("old", _buy(market, "0.1"))).filled
        return market

    async def test_missing_tokens_block_buys_of_that_token(self, tmp_path):
        ledger = open_ledger()
        market = await self._bought_elsewhere(tmp_path, ledger)
        # a carteira nova não tem o JUP que o ledger diz que o bucket tem
        service = _service(tmp_path, _wallet(), market, ledger=ledger)
        await _open(service, "a", JUP)

        events = ledger.conn.execute(
            "SELECT payload FROM events WHERE type = 'reconcile_mismatch'"
        ).fetchall()
        assert len(events) == 1 and JUP.mint in events[0]["payload"]
        reply = await service.submit_order("a", _buy(market, "0.1"))
        assert reply.status == ReplyStatus.REJECTED
        assert "reconcile_mismatch" in reply.reasons[0]
        # outro token segue livre
        await _open(service, "b", BONK)
        assert (await service.submit_order("b", _buy(market, "0.1"))).filled

    async def test_a_wallet_that_holds_the_positions_is_fine(self, tmp_path):
        ledger = open_ledger()
        market = await self._bought_elsewhere(tmp_path, ledger)
        wallet = SimulatedWallet(
            initial={"USDC": Decimal(100), "SOL": Decimal(1), "JUP": Decimal("0.1")}
        )
        service = _service(tmp_path, wallet, market, ledger=ledger)
        await _open(service, "a", JUP)
        assert not service._blocked
        assert (await service.submit_order("a", _buy(market, "0.1"))).filled
