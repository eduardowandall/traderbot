"""A Jupiter Perps de verdade, só leitura (A10): pool, custody, oráculo, posição.

Pelo RPC público da Solana (o conftest tira o `HELIUS_RPC_URL`). Confere que
o layout vendorizado (`perpetuals_idl.json`) ainda bate com as contas, que a
curva de empréstimo dá um número são e que o preço do oráculo da Jupiter
anda junto com o da Price API.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from trader.execution.market import JupiterPriceOracle
from trader.execution.market.jupiter.client import AsyncJupiterClient
from trader.execution.market.perps.idl import decode_account
from trader.execution.market.perps.reader import (
    CUSTODIES,
    JLP_POOL,
    SHORT_COLLATERAL,
    JupiterPerpsReader,
)
from trader.execution.models.perp import PerpTerms
from trader.execution.trade.venues.jupiter_perps.requests import (
    build_transaction,
    close_request,
    open_request,
    stop_request,
)
from trader.shared.models.direction import Direction
from trader.shared.models.mints import SOL_MINT


def _read(work):
    async def run():
        reader = JupiterPerpsReader()
        try:
            return await work(reader)
        finally:
            await reader.aclose()

    return asyncio.run(run())


def test_the_pool_and_the_sol_custody_still_decode():
    async def work(reader):
        return await reader.pool(), await reader.custody(SOL_MINT)

    pool, custody = _read(work)
    assert CUSTODIES[SOL_MINT] in pool["custodies"]
    assert custody.decimals == 9
    # o modelo do paper cobra 0.06% em cada ponta: se mudar, avisa
    assert (custody.open_fee_bps, custody.close_fee_bps) == (6, 6)
    assert 0 < custody.utilization < 1
    assert Decimal(0) < custody.borrow_bps_hour < Decimal(10)


def test_lamports_are_read_without_the_data():
    # A11c F1: o rent da conta de uma posição nova sai daqui
    async def work(reader):
        return await reader.lamports([JLP_POOL, Keypair().pubkey()])

    pool, missing = _read(work)
    assert pool is not None and pool > 0
    assert missing is None


def test_the_oracle_price_is_fresh_and_close_to_the_price_api():
    async def work(reader):
        oracle = await reader.oracle_price(SOL_MINT)
        client = AsyncJupiterClient()
        try:
            prices = await JupiterPriceOracle(client).usd_prices([SOL_MINT])
        finally:
            await client.aclose()
        return oracle, prices[SOL_MINT]

    oracle, price_api = _read(work)
    assert datetime.now(UTC) - oracle.timestamp < timedelta(minutes=2)
    assert abs(oracle.price / price_api - 1) < Decimal("0.05")


def test_a_fresh_wallet_has_no_position():
    terms = PerpTerms(SOL_MINT, Direction.SHORT, Decimal(3))

    async def work(reader):
        return await reader.position(Keypair().pubkey(), terms)

    assert _read(work) is None


# --- A11a: os pedidos, simulados (nada é assinado nem enviado) ----------------

# uma carteira pública de exchange com SOL e USDC: só paga a simulação
EXCHANGE_WALLET = Pubkey.from_string("5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9")


async def _open_short_owner(reader) -> Pubkey | None:
    """O dono de um vendido SOL aberto na Jupiter (para simular fechar/stop)."""
    # `custody` e `collateralCustody` da Position (8 do discriminador + 2 chaves)
    matches = [
        (72, bytes(CUSTODIES[SOL_MINT])),
        (104, bytes(CUSTODIES[SHORT_COLLATERAL])),
    ]
    addresses = (await reader.program_accounts("Position", matches))[:100]
    for data in await reader.accounts(addresses):
        raw = decode_account("Position", data)
        if raw["sizeUsd"]:
            return raw["owner"]
    return None


def test_the_open_requests_simulate_on_mainnet():
    async def work(reader):
        price = (await reader.oracle_price(SOL_MINT)).price
        units = []
        for direction in (Direction.SHORT, Direction.LONG):
            terms = PerpTerms(SOL_MINT, direction, Decimal(2))
            minimum = None
            if direction == Direction.LONG:  # 10 USDC em SOL, menos 5%
                minimum = int(Decimal(10) / price * Decimal("0.95") * 10**9)
            request = open_request(
                EXCHANGE_WALLET,
                terms,
                10_000_000,
                Decimal(20),
                price,
                Decimal("0.02"),
                f"live-{direction}",
                minimum,
            )
            tx = build_transaction(EXCHANGE_WALLET, request, 20_000)
            units.append((await reader.simulate(bytes(tx)))[0])
        return units

    assert all(0 < u < 200_000 for u in _read(work))


def test_close_and_stop_requests_simulate_on_an_open_short():
    async def work(reader):
        owner = await _open_short_owner(reader)
        if owner is None:
            return None
        price = (await reader.oracle_price(SOL_MINT)).price
        custody = await reader.custody(SOL_MINT)
        terms = PerpTerms(SOL_MINT, Direction.SHORT, Decimal(2))
        requests = (
            close_request(owner, terms, price, Decimal("0.02"), "live-close"),
            stop_request(owner, terms, price * Decimal("1.1"), custody, "live-stop"),
        )
        return [
            (await reader.simulate(bytes(build_transaction(owner, r, 20_000))))[0]
            for r in requests
        ]

    units = _read(work)
    if units is None:
        pytest.skip("nenhum vendido SOL aberto na Jupiter agora")
    assert all(0 < u < 200_000 for u in units)
