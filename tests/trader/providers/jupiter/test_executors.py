from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from solders.keypair import Keypair

from trader.paper import SimulatedExecutor, SimulatedWallet, paper_provider
from trader.providers.jupiter.async_jupiter_client import AsyncJupiterClient
from trader.providers.jupiter.async_jupiter_svc import AsyncJupiterProvider
from trader.providers.jupiter.async_rpc_client import AsyncRPCClient
from trader.providers.jupiter.executor import OnChainExecutor


def _on_chain(is_dryrun, rpc=None, client=None):
    return AsyncJupiterProvider.on_chain(
        Keypair(),
        rpc_client=rpc or AsyncMock(spec=AsyncRPCClient),
        jupiter_client=client or AsyncMock(spec=AsyncJupiterClient),
        is_dryrun=is_dryrun,
    )


def test_on_chain_execution_requires_the_key():
    with pytest.raises(ValueError, match="chave"):
        OnChainExecutor(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(("is_dryrun", "tracks"), [(False, True), (True, False)])
def test_on_chain_balances_track_fills_except_in_dry_run(is_dryrun, tracks):
    # dry run não envia a transação: a carteira real não reflete as ordens
    provider = _on_chain(is_dryrun)
    assert provider.balances_track_fills is tracks
    assert provider.native_fee_reserve == Decimal("0.02")


def test_simulated_venue_reserves_sol_only_when_it_charges_fees():
    paper = paper_provider(SimulatedWallet())
    assert paper.balances_track_fills is True
    assert paper.native_fee_reserve == Decimal("0.02")

    replay = SimulatedExecutor(
        SimulatedWallet(), fee_lamports=0, account_rent_lamports=0
    )
    assert replay.native_fee_reserve == Decimal("0")


async def test_provider_closes_its_client_and_the_executor_once():
    rpc, client = AsyncMock(spec=AsyncRPCClient), AsyncMock(spec=AsyncJupiterClient)
    provider = _on_chain(False, rpc, client)
    await provider.aclose()
    client.aclose.assert_awaited_once()
    rpc.aclose.assert_awaited_once()
