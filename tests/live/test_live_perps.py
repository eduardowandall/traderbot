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
    position_address,
)
from trader.execution.models.perp import PerpTerms
from trader.execution.trade.venues.jupiter_perps.requests import (
    USDC as USDC_PUBKEY,
)
from trader.execution.trade.venues.jupiter_perps.requests import (
    Part,
    build_transaction,
    cancel_request,
    close_request,
    open_request,
    stop_request,
    token_account,
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


def test_the_usdc_custody_a_short_borrows_from_decodes():
    # A18: um vendido toma USDC emprestado; a taxa dessa custody é a dele
    custody = _read(lambda reader: reader.custody(SHORT_COLLATERAL))
    assert custody.decimals == 6
    assert 0 <= custody.utilization < 1
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
    async for owner in _open_short_owners(reader):
        return owner
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


# --- A12: fechar uma parte, colateral a mais, cancelar o stop ------------------


async def _open_short_owners(reader, pages: int = 20):
    """Donos de vendidos SOL abertos na Jupiter, de 100 em 100 contas (só
    leitura; as contas fechadas ficam com tamanho 0 e são a maioria)."""
    # `custody` e `collateralCustody` da Position (8 do discriminador + 2 chaves)
    matches = [
        (72, bytes(CUSTODIES[SOL_MINT])),
        (104, bytes(CUSTODIES[SHORT_COLLATERAL])),
    ]
    addresses = await reader.program_accounts("Position", matches)
    for start in range(0, min(len(addresses), pages * 100), 100):
        for data in await reader.accounts(addresses[start : start + 100]):
            raw = decode_account("Position", data)
            if raw["sizeUsd"] > 10 * 10**6:  # uma posição de mais de 10 USD
                yield raw["owner"]


def test_a_partial_close_and_a_top_up_simulate_on_an_open_short():
    async def work(reader):
        price = (await reader.oracle_price(SOL_MINT)).price
        terms = PerpTerms(SOL_MINT, Direction.SHORT, Decimal(2))
        async for owner in _open_short_owners(reader):
            usdc = await reader.token_amount(token_account(owner))
            if usdc < 1_000_000:  # a simulação do aumento gasta 1 USDC dele
                continue
            position = await reader.position(owner, terms)
            part = Part(position.size_usd / 4, position.collateral_usd / 4)
            requests = (
                close_request(owner, terms, price, Decimal("0.02"), "live-part", part),
                open_request(
                    owner,
                    terms,
                    1_000_000,
                    Decimal(0),
                    price,
                    Decimal("0.02"),
                    "live-add",
                ),
            )
            return [
                (await reader.simulate(bytes(build_transaction(owner, r, 20_000))))[0]
                for r in requests
            ]
        return None

    units = _read(work)
    if units is None:
        pytest.skip("nenhum vendido SOL aberto (com USDC na carteira) agora")
    assert all(0 < u < 200_000 for u in units)


async def _cancellable(reader):
    """Um gatilho SOL em USDC aberto: (dono, termos, endereço), ou None."""
    matches = [(72, bytes(CUSTODIES[SOL_MINT])), (136, bytes(USDC_PUBKEY))]
    addresses = (await reader.program_accounts("PositionRequest", matches))[:100]
    for address, data in zip(addresses, await reader.accounts(addresses), strict=True):
        raw = decode_account("PositionRequest", data)
        side = Direction.LONG if raw["side"] == "Long" else Direction.SHORT
        terms = PerpTerms(SOL_MINT, side, Decimal(2))
        trigger = raw["requestType"] == "Trigger" and not raw["executed"]
        # a mesma posição que o venue deriva (colateral da mesma custody)
        if trigger and position_address(raw["owner"], terms) == raw["position"]:
            return raw["owner"], terms, address
    return None


def test_a_leftover_trigger_request_cancels_on_mainnet():
    # o stop de alguém: cancelá-lo, simulado como o dono (nada é enviado)
    async def work(reader):
        found = await _cancellable(reader)
        if found is None:
            return None
        owner, terms, address = found
        tx = build_transaction(owner, cancel_request(owner, terms, address), 20_000)
        return (await reader.simulate(bytes(tx)))[0]

    units = _read(work)
    if units is None:
        pytest.skip("nenhum gatilho SOL em USDC aberto na Jupiter agora")
    assert 0 < units < 200_000


def test_an_open_position_owes_a_sane_borrow():
    # A12: o empréstimo devido (acumulado da custody - o da posição) que o
    # `held` do venue real tira do colateral
    async def work(reader):
        # um vendido toma USDC emprestado: a custody do colateral
        custody = await reader.custody(SHORT_COLLATERAL)
        terms = PerpTerms(SOL_MINT, Direction.SHORT, Decimal(2))
        async for owner in _open_short_owners(reader):
            position = await reader.position(owner, terms)
            if position is not None:
                return position, position.borrow_usd(custody)
        return None

    found = _read(work)
    if found is None:
        pytest.skip("nenhum vendido SOL aberto na Jupiter agora")
    position, owed = found
    assert 0 <= owed < position.collateral_usd
