import pytest
from solders.keypair import Keypair
from solders.litesvm import LiteSVM
from solders.message import Message, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.rpc.responses import (
    RpcResponseContext,
    RpcSimulateTransactionResult,
    SendTransactionResp,
    SimulateTransactionResp,
)
from solders.solders import FailedTransactionMetadata
from solders.system_program import transfer
from solders.transaction import Transaction, VersionedTransaction

from trader.execution.trade.venues.jupiter.async_rpc_client import AsyncRPCClient


class FakeSolanaClient:
    client = LiteSVM()

    async def is_connected(self):
        return True

    async def send_raw_transaction(self, tx: bytes):
        result = FakeSolanaClient.client.send_transaction(Transaction.from_bytes(tx))
        if isinstance(result, FailedTransactionMetadata):
            raise Exception(f"Invalid Result FailedTransactionMetadata {result}")
        signature = result.signature()
        return SendTransactionResp(value=signature)

    async def simulate_transaction(self, tx: VersionedTransaction, **config):
        result = FakeSolanaClient.client.simulate_transaction(tx)
        if isinstance(result, FailedTransactionMetadata):
            return SimulateTransactionResp(
                RpcSimulateTransactionResult(err=result.err()),
                RpcResponseContext(slot=0),  # type: ignore
            )

        return SimulateTransactionResp(
            RpcSimulateTransactionResult(),
            RpcResponseContext(slot=123),  # type: ignore
        )


@pytest.fixture
def mock_signed_transaction():
    keypair = Keypair()
    receiver = Pubkey.new_unique()

    ixs = [
        transfer(
            {
                "from_pubkey": keypair.pubkey(),
                "to_pubkey": receiver,
                "lamports": 1_000_000,
            }
        )
    ]

    client = FakeSolanaClient.client
    client.airdrop(keypair.pubkey(), 1_000_000_000)
    blockhash = client.latest_blockhash()
    msg = Message.new_with_blockhash(ixs, keypair.pubkey(), blockhash)
    tx = VersionedTransaction(msg, [keypair])
    tx.signatures = [keypair.sign_message(to_bytes_versioned(msg))]
    return tx


async def test_send_transaction(mock_signed_transaction):
    resp = await AsyncRPCClient(client=FakeSolanaClient()).send_transaction(
        mock_signed_transaction
    )
    assert resp.value is not None


async def test_simulate_transaction(mock_signed_transaction):
    resp = await AsyncRPCClient(client=FakeSolanaClient()).simulate_transaction(
        mock_signed_transaction
    )
    assert resp.value is not None


def test_missing_rpc_url_is_a_clear_error_even_under_python_O(monkeypatch):
    monkeypatch.delenv("HELIUS_RPC_URL", raising=False)

    with pytest.raises(ValueError, match="HELIUS_RPC_URL"):
        AsyncRPCClient()
