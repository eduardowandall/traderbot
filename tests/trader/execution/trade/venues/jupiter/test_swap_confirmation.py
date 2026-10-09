from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

import pytest
from factories import (
    bonk_quote,
    confirms,
    inspection_passes,
    make_intent,
    memory_gateway,
    signs,
    simulation,
    tx_status,
)
from solders.keypair import Keypair
from solders.signature import Signature
from solders.solders import SendTransactionResp

from trader.execution.market.jupiter.client import AsyncJupiterClient
from trader.execution.models.errors import TransactionSubmittedError
from trader.execution.models.intent import IntentStatus, TxOutcome
from trader.execution.trade.venues.jupiter.provider import AsyncJupiterProvider
from trader.execution.trade.venues.jupiter.rpc import (
    AsyncRPCClient,
    SignedTx,
    TransactionFailedError,
    tx_outcome,
)


class TestTxOutcome:
    """Um status só é lido em `tx_outcome`: a espera e a resolução (A12)."""

    @pytest.mark.parametrize(
        "status, outcome",
        [
            (None, TxOutcome.PENDING),  # ainda não visível para o RPC
            (tx_status("processed"), TxOutcome.PENDING),
            (tx_status("confirmed"), TxOutcome.LANDED),
            (tx_status("finalized"), TxOutcome.LANDED),
            # transações que falham também são confirmadas no bloco
            (tx_status("confirmed", err="InstructionError"), TxOutcome.FAILED),
        ],
    )
    def test_each_status_maps_to_one_outcome(self, status, outcome):
        assert tx_outcome(status) == outcome

    async def test_the_wait_reads_the_recent_status_only(self):
        client = AsyncMock()
        client.get_signature_statuses = AsyncMock(
            return_value=SimpleNamespace(value=[tx_status()])
        )
        rpc = AsyncRPCClient(client=client)
        assert tx_outcome(await rpc.signature_status("1" * 64)) == TxOutcome.LANDED
        assert client.get_signature_statuses.await_args.kwargs == {
            "search_transaction_history": False
        }


@pytest.fixture
def provider():
    return AsyncJupiterProvider.on_chain(
        Keypair(),
        rpc_client=inspection_passes(AsyncMock(spec=AsyncRPCClient)),
        jupiter_client=AsyncMock(spec=AsyncJupiterClient),
        max_priority_fee_lamports=100_000,
    )


class TestWaitForConfirmation:
    async def test_polls_until_confirmed(self, provider, mock_sleep):
        rpc = confirms(provider.executor.rpc_client, None, None, tx_status())
        assert await provider.executor._wait_for_confirmation("sig") is True
        assert rpc.signature_status.await_count == 3

    async def test_failed_transaction_raises_immediately(self, provider, mock_sleep):
        rpc = confirms(provider.executor.rpc_client, tx_status(err="err"))
        with pytest.raises(TransactionFailedError, match="err"):
            await provider.executor._wait_for_confirmation("sig")
        assert rpc.signature_status.await_count == 1

    async def test_transient_errors_are_retried_with_sleep(self, provider):
        confirms(provider.executor.rpc_client, ConnectionError("rpc down"), tx_status())
        with mock.patch("asyncio.sleep") as sleep:
            assert await provider.executor._wait_for_confirmation("sig") is True
        sleep.assert_awaited_once_with(1.0)

    async def test_timeout(self, provider, mock_sleep):
        provider.executor.rpc_client.signature_status = AsyncMock(return_value=None)
        with pytest.raises(TimeoutError):
            await provider.executor._wait_for_confirmation("sig", timeout=0)


class TestNoRetryAfterBroadcast:
    async def test_unconfirmed_send_is_wrapped(self, provider):
        provider.executor._send_signed_transaction = AsyncMock(
            return_value=SendTransactionResp(value=Signature.new_unique())
        )
        provider.executor._wait_for_confirmation = AsyncMock(side_effect=TimeoutError())
        with pytest.raises(TransactionSubmittedError):
            await provider.executor._send_transaction_and_wait_for_confirmation(
                mock.Mock()
            )

    async def test_submitted_error_is_not_retried(self, provider):
        do_swap = AsyncMock(side_effect=TransactionSubmittedError("sent"))
        provider._do_swap = do_swap
        with pytest.raises(TransactionSubmittedError):
            await provider.swap_with_details("mint_in", "mint_out", 1000)
        do_swap.assert_awaited_once()

    async def test_timeout_after_send_sends_only_once(self, provider, mock_sleep):
        provider._get_quote_with_route = AsyncMock()
        provider.executor._get_swap_transaction = AsyncMock()
        provider.executor._get_signed_transaction = AsyncMock()
        provider.executor.rpc_client.send_transaction = AsyncMock(
            return_value=SendTransactionResp(value=Signature.new_unique())
        )
        provider.executor.rpc_client.signature_status = AsyncMock(return_value=None)
        with (
            mock.patch("time.time", side_effect=[0, 100]),
            pytest.raises(TransactionSubmittedError),
        ):
            await provider.swap_with_details("mint_in", "mint_out", 1000)
        provider.executor.rpc_client.send_transaction.assert_awaited_once()

    async def test_pre_broadcast_errors_are_still_retried(self, provider):
        do_swap = AsyncMock(side_effect=[Exception("quote falhou"), "sig"])
        provider._do_swap = do_swap
        assert await provider.swap_with_details("a", "b", 1000) == "sig"
        assert do_swap.await_count == 2


class TestSendErrorsCountAsSubmitted:
    """Um erro no envio pode chegar depois de o nó aceitar a transação."""

    def _ready(self, provider):
        provider._get_quote_with_route = AsyncMock()
        provider.executor._get_swap_transaction = AsyncMock()
        signed = mock.Mock()
        signed.signatures = [Signature.new_unique()]
        provider.executor._get_signed_transaction = AsyncMock(
            return_value=SignedTx(signed, 1)
        )
        return signed

    async def test_a_send_that_times_out_is_never_resent(self, provider, mock_sleep):
        signed = self._ready(provider)
        send = AsyncMock(side_effect=TimeoutError("sem resposta do RPC"))
        provider.executor.rpc_client.send_transaction = send

        with pytest.raises(TransactionSubmittedError) as ex:
            await provider.swap_with_details("mint_in", "mint_out", 1000)

        send.assert_awaited_once()
        provider._get_quote_with_route.assert_awaited_once()  # nenhuma nova quote
        # a assinatura vai para o ledger (UNCONFIRMED), para conferir depois
        assert ex.value.signature == str(signed.signatures[0])

    async def test_a_failed_simulation_is_still_retried(self, provider, mock_sleep):
        self._ready(provider)
        provider.executor.rpc_client.simulate_transaction = AsyncMock(
            side_effect=[Exception("simulação falhou"), simulation()]
        )
        provider.executor.rpc_client.send_transaction = AsyncMock(
            return_value=SendTransactionResp(value=Signature.new_unique())
        )
        confirms(provider.executor.rpc_client)

        await provider.swap_with_details("mint_in", "mint_out", 1000)

        assert provider._get_quote_with_route.await_count == 2
        provider.executor.rpc_client.send_transaction.assert_awaited_once()


class TestFailedOnChain:
    """Confirmada com erro (ex: slippage): re-tentável, e termina FAILED."""

    def _failing_executor(self, provider):
        rpc = provider.executor.rpc_client
        signs(rpc)
        rpc.send_transaction = AsyncMock(
            return_value=SendTransactionResp(value=Signature.new_unique())
        )
        rpc.signature_status = AsyncMock(return_value=tx_status(err="slippage"))
        provider.jupiter_client.get_quote = AsyncMock(return_value=bonk_quote())
        provider.jupiter_client.get_swap_transaction = AsyncMock()
        return rpc

    async def test_it_is_retried_with_the_slippage_escalation(
        self, provider, mock_sleep
    ):
        rpc = self._failing_executor(provider)
        with pytest.raises(RuntimeError, match="falhou na rede"):
            await provider.swap_with_details("in", "out", 1000, slippage_bps=50)
        assert rpc.send_transaction.await_count == 3  # nunca UNCONFIRMED
        slippages = [
            c.args[3] for c in provider.jupiter_client.get_quote.await_args_list
        ]
        assert slippages == [50, 50, 75]

    async def test_the_intent_ends_failed_and_trading_goes_on(
        self, provider, mock_sleep
    ):
        self._failing_executor(provider)
        gateway = memory_gateway()
        intent = make_intent()
        with pytest.raises(RuntimeError):
            await gateway.submit(
                intent, lambda: provider.swap_with_details("in", "out", 1000)
            )
        record = gateway.ledger.get(intent.intent_id)
        assert record is not None and record.status == IntentStatus.FAILED
        assert gateway.ledger.policy_state().unresolved_intent_ids == ()
