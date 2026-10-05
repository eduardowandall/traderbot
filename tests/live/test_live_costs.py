"""B9 ao vivo: o teto da priority fee na transação real e os custos medidos."""

import asyncio
from decimal import Decimal

from live_helpers import invoke_json
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from trader.shared.market.jupiter.async_jupiter_client import AsyncJupiterClient
from trader.shared.models import SOLANA_MINTS
from trader.shared.paths import PROJECT_ROOT

SOL = SOLANA_MINTS.get_by_symbol("SOL")
USDC = SOLANA_MINTS.get_by_symbol("USDC")
RANDOM_SPEC = str(PROJECT_ROOT / "docs" / "examples" / "spec-random.json")
COMPUTE_BUDGET = Pubkey.from_string("ComputeBudget111111111111111111111111111111")
CAP = 20_000


def _priority_lamports(tx: VersionedTransaction) -> int:
    """`SetComputeUnitLimit` x `SetComputeUnitPrice` (micro-lamports) / 1e6."""
    keys = tx.message.account_keys
    limit = price = 0
    for ix in tx.message.instructions:
        if keys[ix.program_id_index] != COMPUTE_BUDGET:
            continue
        data = bytes(ix.data)
        if data[0] == 2:  # SetComputeUnitLimit(u32)
            limit = int.from_bytes(data[1:5], "little")
        elif data[0] == 3:  # SetComputeUnitPrice(u64)
            price = int.from_bytes(data[1:9], "little")
    return limit * price // 1_000_000


def test_jupiter_keeps_the_priority_fee_under_the_policy_cap():
    async def build():
        client = AsyncJupiterClient()
        try:
            quote = await client.get_quote(USDC.mint, SOL.mint, USDC.ui_to_raw("5"))
            return await client.get_swap_transaction(quote, Keypair().pubkey(), CAP)
        finally:
            await client.aclose()

    tx = asyncio.run(build())

    # a Jupiter aceita o corpo e respeita o `maxLamports` (+1 de arredondamento)
    assert _priority_lamports(tx) <= CAP + 1


def test_the_backtest_measures_sol_usdc_costs_on_jupiter():
    result = invoke_json("backtest", RANDOM_SPEC, "--candles", "100")

    measured = result["measured_costs"]
    assert measured is not None
    # SOL-USDC a 5 USD: pool profundo, ida e volta de poucos bps
    assert Decimal("0") <= Decimal(measured["fee_bps"]) < Decimal("25")
    assert Decimal(result["fee_bps"]) == Decimal(measured["fee_bps"])
    # base + teto padrão (105000 lamports): centavos a qualquer preço do SOL
    assert Decimal("0.001") < Decimal(result["network_fee_usd"]) < Decimal("0.2")
    assert "round_trip_costs" in result
