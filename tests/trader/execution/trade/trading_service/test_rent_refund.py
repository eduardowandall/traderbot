"""A15: um bucket encerrado sem posição fecha a conta do token e o rent volta."""

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from factories import events_of, inspection_passes, memory_gateway, spot_provider
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.rpc.responses import SendTransactionResp
from solders.signature import Signature
from solders.solders import TOKEN_PROGRAM_ID
from solders.transaction import VersionedTransaction
from spl.token.instructions import get_associated_token_address

from trader.backtest import Tick
from trader.backtest.replay import ReplayQuoteClient
from trader.execution.models.errors import TransactionFailedOnChainError
from trader.execution.trade.ledger.events import RENT_REFUND, RENT_REFUND_SENT
from trader.execution.trade.policy import Policy
from trader.execution.trade.trading_service.service import TradeService
from trader.execution.trade.venues.jupiter.executor import OnChainExecutor
from trader.execution.trade.venues.jupiter.provider import AsyncJupiterProvider
from trader.execution.trade.venues.jupiter.rpc import (
    AsyncRPCClient,
    SignedTx,
    TokenAccount,
)
from trader.execution.trade.venues.paper import SimulatedWallet, paper_provider
from trader.execution.trade.venues.paper.executor import (
    DEFAULT_ACCOUNT_RENT_LAMPORTS,
    DEFAULT_FEE_LAMPORTS,
)
from trader.execution.trade.venues.spot import SpotVenue
from trader.shared.models import SOLANA_MINTS, OrderSide
from trader.shared.trading_service.protocol import OrderRequest, ReplyStatus

USDC = SOLANA_MINTS.get_by_symbol("USDC")
SOL = SOLANA_MINTS.get_by_symbol("SOL")
JUP = SOLANA_MINTS.get_by_symbol("JUP")
T0 = datetime(2026, 10, 6, 6, 0)
RENT, FEE = DEFAULT_ACCOUNT_RENT_LAMPORTS, DEFAULT_FEE_LAMPORTS
NET_USD = Decimal(RENT - FEE) / Decimal(10**9) * 100  # SOL a 100 USD
LOOSE = Policy(
    max_trade_usd=Decimal(1000),
    max_daily_notional_usd=Decimal(100000),
    max_trades_per_hour=100000,
    max_trades_per_hour_per_bucket=100000,
    max_daily_loss_usd=Decimal(100000),
)


class _Oracle:
    async def usd_prices(self, mints):
        return {SOL.mint: Decimal(100)} if SOL.mint in mints else {}


def _setup(jup="0"):
    wallet = SimulatedWallet(
        initial={"USDC": Decimal(100), "SOL": Decimal(1), "JUP": Decimal(jup)}
    )
    client = ReplayQuoteClient(USDC, Decimal(0))
    client.tick = Tick(T0, Decimal("0.5"))
    provider = paper_provider(wallet, jupiter_client=client)
    gateway = memory_gateway(LOOSE)
    service = TradeService(SpotVenue(provider), gateway, mode="paper", prices=_Oracle())
    return service, wallet


async def _round_trip(service, name):
    """Compra 10 JUP e vende tudo: a primeira compra abre a conta do token."""
    await service.open_bucket(name, USDC.mint, JUP.mint)
    buy = OrderRequest(OrderSide.BUY, Decimal(10), Decimal("0.5"), "teste")
    assert (await service.submit_order(name, buy)).status == ReplyStatus.FILLED
    position = service._bucket(name).account.book.position
    sell = OrderRequest(
        OrderSide.SELL, position.entry_order.quantity, Decimal("0.5"), "teste"
    )
    assert (await service.submit_order(name, sell)).status == ReplyStatus.FILLED


class TestClosesTheAccountOfARetiredBucket:
    async def test_the_rent_comes_back_to_the_bucket(self):
        service, wallet = _setup()
        await _round_trip(service, "a")
        sol_before = wallet.raw_balance(SOL.mint)
        realized_before = service._bucket("a").account.book.realized_usd
        service.retire("a", "teste")

        refund = await service.close_token_account("a")

        assert refund is not None
        assert (refund.refund_lamports, refund.fee_lamports) == (RENT, FEE)
        assert wallet.needs_account(JUP.mint)  # a conta foi fechada
        assert wallet.raw_balance(SOL.mint) == sol_before + RENT - FEE
        ledger = service.gateway.ledger
        [sent] = events_of(ledger, RENT_REFUND_SENT)
        [done] = events_of(ledger, RENT_REFUND)
        assert sent["signature"] == done["signature"] == refund.signature
        assert done["account"] == "paper:a"
        assert Decimal(done["net_usd"]) == NET_USD
        totals = ledger.pnl_totals("paper:a")
        assert totals.rent_refund_lamports == RENT
        book = service._bucket("a").account.book
        assert book.realized_usd == realized_before + NET_USD
        assert "rent devolvido" in book.summary()

    async def test_the_refund_comes_off_the_round_trip_cost(self):
        service, _ = _setup()
        await _round_trip(service, "a")
        before = service.gateway.ledger.round_trip_costs("paper:a").cost_usd
        service.retire("a", "teste")

        await service.close_token_account("a")

        after = service.gateway.ledger.round_trip_costs("paper:a").cost_usd
        assert after == before - NET_USD

    async def test_once_per_bucket(self):
        service, _ = _setup()
        await _round_trip(service, "a")
        service.retire("a", "teste")
        await service.close_token_account("a")

        assert await service.close_token_account("a") is None
        assert len(events_of(service.gateway.ledger, RENT_REFUND)) == 1


class TestLeavesTheAccountAlone:
    async def test_while_the_bucket_is_active(self):
        service, wallet = _setup()
        await _round_trip(service, "a")

        assert await service.close_token_account("a") is None
        assert not wallet.needs_account(JUP.mint)

    async def test_while_another_bucket_is_active_on_the_token(self):
        service, wallet = _setup()
        await _round_trip(service, "a")
        await service.open_bucket("b", USDC.mint, JUP.mint)
        service.retire("a", "teste")

        assert await service.close_token_account("a") is None
        assert not wallet.needs_account(JUP.mint)

    async def test_while_a_bucket_holds_the_token(self):
        service, wallet = _setup()
        await _round_trip(service, "a")
        await service.open_bucket("b", USDC.mint, JUP.mint)
        buy = OrderRequest(OrderSide.BUY, Decimal(4), Decimal("0.5"), "teste")
        await service.submit_order("b", buy)
        service.retire("b", "teste")  # encerrado, mas ainda com a posição
        service.retire("a", "teste")
        # a carteira também recusaria (tem JUP): o ledger decide antes
        spot_provider(service).close_token_account = AsyncMock()

        assert await service.close_token_account("a") is None
        spot_provider(service).close_token_account.assert_not_awaited()
        assert not wallet.needs_account(JUP.mint)

    async def test_an_account_the_bot_did_not_open(self):
        # a carteira já tinha JUP: a compra não pagou rent, a conta é do dono
        service, wallet = _setup(jup="0")
        wallet.apply_swap(USDC.mint, 1, JUP.mint, 1)  # o dono abriu a conta
        wallet.apply_swap(JUP.mint, 1, USDC.mint, 0)
        await _round_trip(service, "a")
        service.retire("a", "teste")

        assert await service.close_token_account("a") is None
        assert not wallet.needs_account(JUP.mint)

    def test_a_wallet_account_with_tokens(self):
        wallet = SimulatedWallet(initial={"SOL": Decimal(1), "JUP": Decimal(1)})

        assert not wallet.close_account(JUP.mint, RENT, FEE, "sig")
        assert not wallet.close_account(SOL.mint, RENT, FEE, "sig")
        assert not wallet.needs_account(JUP.mint)


class TestWhoGetsTheRefund:
    async def test_the_bucket_that_paid_the_rent(self):
        service, _ = _setup()
        await _round_trip(service, "a")
        await service.open_bucket("b", USDC.mint, JUP.mint)
        service.retire("a", "teste")
        await service.close_token_account("a")  # "b" ainda ativo: fica aberta
        service.retire("b", "teste")

        await service.close_token_account("b")

        [done] = events_of(service.gateway.ledger, RENT_REFUND)
        assert done["account"] == "paper:a"
        assert service.gateway.ledger.rent_payer("paper:", JUP.mint) is None


class TestUnsettledCloses:
    async def test_a_close_that_failed_on_chain_books_its_fee(self):
        service, wallet = _setup()
        await _round_trip(service, "a")
        service.retire("a", "teste")
        spot_provider(service).close_token_account = AsyncMock(
            side_effect=TransactionFailedOnChainError("falhou", signature="sig-x")
        )

        refund = await service.close_token_account("a")

        assert refund is not None
        assert (refund.refund_lamports, refund.fee_lamports) == (0, FEE)
        assert not wallet.needs_account(JUP.mint)

    async def test_a_send_without_outcome_is_resolved_on_the_next_sweep(self):
        service, wallet = _setup()
        await _round_trip(service, "a")
        service.retire("a", "teste")
        close = spot_provider(service).close_token_account

        async def dies_after_send(mint, announce):
            await close(mint, announce)
            raise RuntimeError("processo morto")

        spot_provider(service).close_token_account = dies_after_send
        with pytest.raises(RuntimeError):
            await service.close_token_account("a")
        ledger = service.gateway.ledger
        assert len(ledger.pending_rent_refunds("paper:")) == 1

        await service.resolve_rent_refunds()

        assert ledger.pending_rent_refunds("paper:") == []
        [done] = events_of(ledger, RENT_REFUND)
        assert (done["refund_lamports"], done["fee_lamports"]) == (RENT, FEE)
        assert wallet.needs_account(JUP.mint)


class TestOnChainClose:
    def _executor(self, account_amount=0):
        keypair = Keypair()
        address = get_associated_token_address(
            keypair.pubkey(), JUP.pubkey, TOKEN_PROGRAM_ID
        )
        account = TokenAccount(address, TOKEN_PROGRAM_ID, 2_039_280, account_amount)

        async def sign(instructions, signer):
            message = MessageV0.try_compile(
                signer.pubkey(), instructions, [], Hash.default()
            )
            return SignedTx(VersionedTransaction(message, [signer]), 1_000)

        rpc = AsyncMock(spec=AsyncRPCClient)
        rpc.associated_token_accounts = AsyncMock(return_value=[account])
        rpc.sign_instructions = AsyncMock(side_effect=sign)
        rpc.send_transaction = AsyncMock(
            return_value=SendTransactionResp(value=Signature.new_unique())
        )
        rpc.check_signature_is_confirmed = AsyncMock(return_value=True)
        rpc.get_confirmed_transaction = AsyncMock(
            return_value=SimpleNamespace(meta=SimpleNamespace(fee=5000))
        )
        inspection_passes(rpc)  # a simulação com a carteira (A11b: um caminho só)
        executor = OnChainExecutor(keypair, rpc, AsyncMock(), 100_000)
        return executor, rpc, address

    async def test_closes_the_empty_account_announcing_before_the_send(self):
        executor, rpc, address = self._executor()
        order = []
        rpc.send_transaction.side_effect = lambda tx: (
            order.append("send"),
            rpc.send_transaction.return_value,
        )[1]

        # o provider lê a taxa da transação confirmada
        provider = AsyncJupiterProvider(executor, AsyncMock())
        refund = await provider.close_token_account(
            JUP.mint, lambda sent: order.append(("announce", sent.out_amount))
        )

        assert order == [("announce", 2_039_280), "send"]
        assert refund is not None
        assert (refund.refund_lamports, refund.fee_lamports) == (2_039_280, 5000)
        [instruction] = rpc.sign_instructions.await_args.args[0]
        assert instruction.program_id == TOKEN_PROGRAM_ID
        assert instruction.accounts[0].pubkey == address
        assert bytes(instruction.data) == bytes([9])  # CloseAccount
        rpc.simulate_transaction.assert_awaited_once()

    async def test_an_account_with_tokens_is_not_closed(self):
        executor, rpc, _ = self._executor(account_amount=1)

        assert await executor.close_token_account(JUP.mint, lambda sent: None) is None
        rpc.sign_instructions.assert_not_awaited()
        rpc.send_transaction.assert_not_awaited()


class TestSameGateAsSwaps:
    async def test_an_unresolved_intent_holds_the_close_until_it_clears(self):
        from factories import make_intent

        from trader.execution.models.intent import PolicyDecision

        service, wallet = _setup()
        await _round_trip(service, "a")
        service.retire("a", "teste")
        ledger = service.gateway.ledger
        intent = make_intent(account="paper:b")
        ledger.record_intent(intent, PolicyDecision(True))
        ledger.mark_unconfirmed(intent.intent_id, "interrompida")

        assert await service.close_token_account("a") is None
        assert events_of(ledger, RENT_REFUND_SENT) == []

        ledger.mark_failed(intent.intent_id, "resolvida")
        assert await service.close_token_account("a") is not None  # tenta de novo
        assert wallet.needs_account(JUP.mint)
