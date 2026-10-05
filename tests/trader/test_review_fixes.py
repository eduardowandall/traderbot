"""Correções da revisão de lacunas (fundação e precisão de trades e custos)."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from factories import make_intent, make_spec, memory_gateway, mock_provider, open_ledger
from solana.rpc.commitment import Confirmed

from trader.backtest import Tick
from trader.backtest.replay import ReplayQuoteClient
from trader.execution import TradeGateway
from trader.execution.account import AsyncAccount
from trader.execution.balances import WalletBalances
from trader.models import SOLANA_MINTS, Order, OrderSide, Position, SwapResult
from trader.models.account_data import MintBalance
from trader.models.costs import TradeCosts
from trader.models.errors import SwapRejectedError
from trader.models.intent import IntentStatus
from trader.paper import SimulatedWallet, paper_provider
from trader.policy import Policy
from trader.providers.jupiter.async_rpc_client import AsyncRPCClient
from trader.runners.trade_runner import TradeRunner
from trader.strategy_spec.validate import SpecLimits
from trader.trading_service.protocol import OrderRequest, TradeServiceError
from trader.trading_service.remote import RemoteTradeClient
from trader.trading_service.service import TradeService

USDC = SOLANA_MINTS.get_by_symbol("USDC")
SOL = SOLANA_MINTS.get_by_symbol("SOL")
JUP = SOLANA_MINTS.get_by_symbol("JUP")
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
LOOSE = Policy(max_trade_usd=Decimal(1000), max_trades_per_hour=1000)


def test_rpc_reads_at_confirmed_like_the_swap_confirmation(monkeypatch):
    # em Finalized, o saldo logo depois de um swap ainda era o de antes
    monkeypatch.setenv("HELIUS_RPC_URL", "https://rpc.invalid")
    rpc = AsyncRPCClient()
    try:
        assert rpc.client._commitment == Confirmed
    finally:
        asyncio.run(rpc.aclose())


async def test_a_sell_rereads_the_wallet():
    provider = mock_provider()
    provider.get_account_balance = AsyncMock(
        return_value=[MintBalance(mint=SOL.pubkey, available=Decimal("1"))]
    )
    provider.sell = AsyncMock(
        return_value=SwapResult("s", SOL.mint, USDC.mint, SOL.ui_to_raw("0.5"), 50)
    )
    account = AsyncAccount(provider, USDC.pubkey, SOL.pubkey, memory_gateway())
    entry = Order(
        "buy", USDC.mint, SOL.mint, Decimal("0.5"), Decimal(100), OrderSide.BUY, T0
    )
    account.book.open(entry)
    await account.get_balance(SOL.pubkey)  # cache quente, de antes

    await account.sell(Decimal(100), Decimal("0.5"))

    assert provider.get_account_balance.await_count == 2


async def test_provider_rejections_dont_trip_the_breaker():
    gateway = memory_gateway()

    async def rejected():
        raise SwapRejectedError("impacto de preço 3% acima do limite 1%")

    for _ in range(5):
        with pytest.raises(SwapRejectedError):
            await gateway.submit(make_intent(), rejected)
    records = gateway.ledger.list_intents()
    assert all(r.status == IntentStatus.REJECTED for r in records)
    assert all("impacto de preço" in (r.error or "") for r in records)
    assert gateway.ledger.policy_state().consecutive_failures == 0


async def test_a_read_that_overlaps_a_fill_is_not_cached():
    release = asyncio.Event()
    reads = []

    async def slow_balances():
        reads.append(1)
        await release.wait()
        return [MintBalance(mint=USDC.pubkey, available=Decimal(100))]

    provider = mock_provider()
    provider.get_account_balance = AsyncMock(side_effect=slow_balances)
    wallet = WalletBalances(provider)
    pending = asyncio.create_task(wallet.get(USDC.pubkey))
    await asyncio.sleep(0)
    wallet.invalidate()  # um fill de outro bucket, no meio da leitura
    release.set()
    assert await pending == Decimal(100)

    await wallet.get(USDC.pubkey)
    assert len(reads) == 2  # o resultado antigo não ficou no cache


def test_a_leg_without_a_sol_price_uses_the_other_legs():
    costs = TradeCosts("onchain", fee_lamports=1_000_000, actual_in_amount=1)
    entry = Order(
        "b",
        USDC.mint,
        JUP.mint,
        Decimal(10),
        Decimal(1),
        OrderSide.BUY,
        T0,
        quote_amount=Decimal(10),
        costs=costs,
        sol_usd=None,
    )
    exit_ = Order(
        "s",
        USDC.mint,
        JUP.mint,
        Decimal(10),
        Decimal("1.1"),
        OrderSide.SELL,
        T0,
        quote_amount=Decimal(11),
        costs=costs,
        sol_usd=Decimal(100),
        sol_in_quote=Decimal(100),
    )
    pnl = Position(entry, exit_).realized_pnl_detail()
    assert pnl is not None
    # 0.001 SOL por perna, as duas a 100 USD/SOL: 0.2 USD de custos
    assert pnl.costs_usd == Decimal("0.2")


def _paper_service(ledger=None):
    quotes = ReplayQuoteClient(USDC, Decimal(0))
    quotes.tick = Tick(T0, Decimal(100))
    wallet = SimulatedWallet(initial={"USDC": Decimal(100), "SOL": Decimal(1)})
    gateway = TradeGateway(ledger or open_ledger(), LOOSE, False)
    return TradeService(paper_provider(wallet, jupiter_client=quotes), gateway)


async def test_a_sell_keeps_the_key_it_was_sent_with():
    service = _paper_service()
    await service.open_bucket("a", USDC.mint, JUP.mint)
    buy = OrderRequest(OrderSide.BUY, Decimal("0.2"), Decimal(100))
    assert (await service.submit_order("a", buy)).filled
    sell = OrderRequest(
        OrderSide.SELL, Decimal("0.1"), Decimal(100), idempotency_key="k1"
    )
    assert (await service.submit_order("a", sell)).filled
    again = await service.submit_order("a", sell)  # reenvio de uma venda parcial
    assert not again.filled
    keys = [r.intent.idempotency_key for r in service.gateway.ledger.list_intents()]
    assert keys.count("k1") == 1


async def test_a_restarted_trade_runner_is_found_again():
    spec = make_spec(ttl_days=5, expires_at=None)
    ledger = open_ledger()
    limits = SpecLimits(Decimal(1000))
    first = TradeRunner(_paper_service(ledger), "paper", limits)
    server = await first.start()
    current = {
        "host": "127.0.0.1",
        "port": server.sockets[0].getsockname()[1],
        "token": first.token,
    }
    client = RemoteTradeClient(
        current["host"],
        current["port"],
        current["token"],
        spec,
        backoff_initial=0.01,
        resolve=lambda: current,
    )
    await client.open()
    await client.aclose()
    server.close()
    await server.wait_closed()

    # reiniciado: outra porta, outro token, o mesmo ledger
    second = TradeRunner(_paper_service(ledger), "paper", limits)
    async with await second.start() as server2:
        current.update(port=server2.sockets[0].getsockname()[1], token=second.token)
        snapshot = await client.bucket()
        assert snapshot.bucket == client.bucket_name
        await client.aclose()


async def test_the_client_gives_up_so_the_bot_can_back_off(monkeypatch):
    # cada conexão recusada custa ~2s no Windows: uma tentativa basta aqui
    monkeypatch.setattr("trader.trading_service.remote.RECONNECT_ATTEMPTS", 1)
    client = RemoteTradeClient("127.0.0.1", 9, "t", {}, backoff_initial=0.001)
    with pytest.raises(TradeServiceError, match="fora do ar"):
        await client.bucket()
