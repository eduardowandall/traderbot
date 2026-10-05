from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from factories import bonk_quote
from solders.keypair import Keypair
from solders.solders import TransactionConfirmationStatus

from trader.execution.market.jupiter.async_jupiter_client import AsyncJupiterClient
from trader.execution.models.intent import SentTx, TxOutcome, send_hook
from trader.execution.trade.venues.jupiter.async_jupiter_svc import AsyncJupiterProvider
from trader.execution.trade.venues.jupiter.async_rpc_client import AsyncRPCClient
from trader.execution.trade.venues.jupiter.executor import OnChainExecutor
from trader.execution.trade.venues.paper import (
    SimulatedExecutor,
    SimulatedWallet,
    paper_provider,
)


def _on_chain(rpc=None, client=None):
    return AsyncJupiterProvider.on_chain(
        Keypair(),
        rpc_client=rpc or AsyncMock(spec=AsyncRPCClient),
        jupiter_client=client or AsyncMock(spec=AsyncJupiterClient),
        max_priority_fee_lamports=100_000,
    )


def test_on_chain_execution_requires_the_key():
    with pytest.raises(ValueError, match="chave"):
        OnChainExecutor(None, None, None, 0)  # type: ignore[arg-type]


def test_on_chain_reserves_sol_for_fees():
    assert _on_chain().native_fee_reserve == Decimal("0.02")


def test_simulated_venue_reserves_sol_only_when_it_charges_fees():
    paper = paper_provider(SimulatedWallet())
    assert paper.native_fee_reserve == Decimal("0.02")

    replay = SimulatedExecutor(
        SimulatedWallet(), fee_lamports=0, account_rent_lamports=0
    )
    assert replay.native_fee_reserve == Decimal("0")


async def test_provider_closes_its_client_and_the_executor_once():
    rpc, client = AsyncMock(spec=AsyncRPCClient), AsyncMock(spec=AsyncJupiterClient)
    provider = _on_chain(rpc, client)
    await provider.aclose()
    client.aclose.assert_awaited_once()
    rpc.aclose.assert_awaited_once()


async def test_on_chain_execution_order(mock_rpc_client, mock_jupiter_client):
    # monta, assina, simula, envia e confirma; os custos vêm depois, da
    # transação confirmada (só quando o ledger já marcou EXECUTED)
    keypair = Keypair()
    executor = OnChainExecutor(keypair, mock_rpc_client, mock_jupiter_client, 100_000)
    quote = bonk_quote()

    result = await executor.execute(quote.inputMint, quote.outputMint, quote)
    await executor.fetch_costs(result)

    assert [c[0] for c in mock_jupiter_client.mock_calls] == ["get_swap_transaction"]
    assert [c[0] for c in mock_rpc_client.mock_calls if c[0].isidentifier()] == [
        "sign_transaction",
        "wallet_state",
        "simulate_transaction",
        "send_transaction",
        "check_signature_is_confirmed",
        "get_confirmed_transaction",
    ]
    assert (result.in_amount, result.out_amount) == (50_000_000, 5_000_000)


class TestOutcome:
    """A3: o que a rede diz de um envio gravado."""

    def _executor(self, status, height=0):
        rpc = AsyncMock(spec=AsyncRPCClient)
        rpc.signature_status = AsyncMock(return_value=status)
        rpc.finalized_block_height = AsyncMock(return_value=height)
        return OnChainExecutor(Keypair(), rpc, AsyncMock(), 0)

    def _sent(self, last_valid=100):
        return SentTx("sig", "a", "b", 1, 2, last_valid)

    @pytest.mark.parametrize(
        ("err", "level", "outcome"),
        [
            ("InstructionError", TransactionConfirmationStatus.Confirmed, "failed"),
            (None, TransactionConfirmationStatus.Confirmed, "landed"),
            (None, TransactionConfirmationStatus.Finalized, "landed"),
            (None, TransactionConfirmationStatus.Processed, "pending"),
        ],
    )
    async def test_a_seen_signature(self, err, level, outcome):
        status = SimpleNamespace(err=err, confirmation_status=level)
        assert await self._executor(status).outcome(self._sent()) == outcome

    @pytest.mark.parametrize(
        ("height", "last_valid", "outcome"),
        [(101, 100, "expired"), (100, 100, "pending"), (500, None, "pending")],
    )
    async def test_an_unseen_signature_expires_with_its_blockhash(
        self, height, last_valid, outcome
    ):
        executor = self._executor(None, height)
        assert await executor.outcome(self._sent(last_valid)) == outcome

    async def test_the_provider_reads_an_rpc_error_as_pending(self):
        provider = _on_chain()
        provider.executor.outcome = AsyncMock(side_effect=RuntimeError("rpc caiu"))
        assert await provider.send_outcome(self._sent()) == TxOutcome.PENDING


async def test_the_send_is_announced_after_the_checks_and_before_the_send(
    mock_rpc_client, mock_jupiter_client
):
    executor = OnChainExecutor(Keypair(), mock_rpc_client, mock_jupiter_client, 0)
    quote = bonk_quote()
    seen = []

    def hook(sent):
        seen.append(sent)
        mock_rpc_client.send_transaction.assert_not_awaited()
        mock_rpc_client.simulate_transaction.assert_awaited_once()

    token = send_hook.set(hook)
    try:
        result = await executor.execute(quote.inputMint, quote.outputMint, quote)
    finally:
        send_hook.reset(token)

    [sent] = seen
    assert (sent.input_mint, sent.output_mint) == (
        result.input_mint,
        result.output_mint,
    )
    assert sent.last_valid_block_height == 1_000
    assert (sent.in_amount, sent.out_amount) == (50_000_000, 5_000_000)


async def test_a_failing_send_log_means_no_send(mock_rpc_client, mock_jupiter_client):
    executor = OnChainExecutor(Keypair(), mock_rpc_client, mock_jupiter_client, 0)
    quote = bonk_quote()

    def hook(sent):
        raise OSError("disco cheio")

    token = send_hook.set(hook)
    try:
        with pytest.raises(OSError):
            await executor.execute(quote.inputMint, quote.outputMint, quote)
    finally:
        send_hook.reset(token)
    mock_rpc_client.send_transaction.assert_not_awaited()


async def test_on_chain_refuses_to_send_what_nobody_records(
    mock_rpc_client, mock_jupiter_client
):
    executor = OnChainExecutor(Keypair(), mock_rpc_client, mock_jupiter_client, 0)
    quote = bonk_quote()
    token = send_hook.set(None)  # fora do gateway, sem o `send_hook` dele
    try:
        with pytest.raises(LookupError, match="sem registro"):
            await executor.execute(quote.inputMint, quote.outputMint, quote)
    finally:
        send_hook.reset(token)
    mock_rpc_client.send_transaction.assert_not_awaited()
