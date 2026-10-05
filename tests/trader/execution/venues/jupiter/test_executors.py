from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from factories import bonk_quote
from solders.keypair import Keypair

from trader.execution.venues.jupiter.async_jupiter_svc import AsyncJupiterProvider
from trader.execution.venues.jupiter.async_rpc_client import AsyncRPCClient
from trader.execution.venues.jupiter.executor import OnChainExecutor
from trader.execution.venues.paper import (
    SimulatedExecutor,
    SimulatedWallet,
    paper_provider,
)
from trader.shared.market.jupiter.async_jupiter_client import AsyncJupiterClient


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
