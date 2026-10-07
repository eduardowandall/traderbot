"""A Jupiter Perps lida da rede (A10): o decodificador, a curva e o leitor.

As contas são bytes de verdade da mainnet (`perps_accounts.json`, capturados
em 2026-10-07; o dono da posição zerado), então nada aqui sai para a rede.
"""

import base64
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from solders.keypair import Keypair

from trader.execution.market.perps.idl import AccountFormatError, decode_account
from trader.execution.market.perps.reader import (
    CUSTODIES,
    JLP_POOL,
    JumpRate,
    JupiterPerpsReader,
    PerpsReadError,
    custody_state,
    position_address,
    venue_position,
)
from trader.execution.models.perp import PerpTerms
from trader.shared.models import SOLANA_MINTS
from trader.shared.models.direction import Direction
from trader.shared.models.mints import SOL_MINT

USDC = SOLANA_MINTS.get_by_symbol("USDC").mint
ACCOUNTS = {
    name: base64.b64decode(data)
    for name, data in json.loads(
        (Path(__file__).with_name("perps_accounts.json")).read_text(encoding="utf-8")
    )["accounts"].items()
}
SHORT = PerpTerms(SOL_MINT, Direction.SHORT, Decimal(3))
LONG = PerpTerms(SOL_MINT, Direction.LONG, Decimal(3))


def test_the_pool_lists_the_sol_custody():
    pool = decode_account("Pool", ACCOUNTS["Pool"])
    assert pool["name"] == "Pool"
    assert CUSTODIES[SOL_MINT] in pool["custodies"]


def test_the_sol_custody_has_the_fees_and_curve_we_model():
    custody = custody_state(decode_account("Custody", ACCOUNTS["Custody"]), SOL_MINT)
    assert custody.decimals == 9
    assert (custody.open_fee_bps, custody.close_fee_bps) == (6, 6)  # 0.06%
    assert 0 < custody.utilization < 1
    curve = custody.jump_rate
    assert curve.min_bps < curve.target_bps < curve.max_bps
    assert 0 < custody.borrow_bps_hour < 1  # bps do tamanho por hora


def test_a_custody_of_another_mint_is_refused():
    with pytest.raises(PerpsReadError, match="não de"):
        custody_state(decode_account("Custody", ACCOUNTS["UsdcCustody"]), SOL_MINT)


@pytest.mark.parametrize(
    ("utilization", "annual"),
    [("0", 1000), ("0.4", 2250), ("0.8", 3500), ("0.9", 9250), ("1", 15000)],
)
def test_the_jump_rate_has_two_slopes(utilization, annual):
    # a curva da custody SOL em 2026-10: 10% -> 35% no alvo de 80% -> 150%
    curve = JumpRate(Decimal(1000), Decimal(15000), Decimal(3500), Decimal("0.8"))
    assert curve.annual_bps(Decimal(utilization)) == annual


def test_the_doves_feed_and_a_position_decode():
    feed = decode_account("AgPriceFeed", ACCOUNTS["AgPriceFeed"])
    assert str(feed["mint"]) == SOL_MINT and feed["price"] > 0
    raw = decode_account("Position", ACCOUNTS["Position"])
    position = venue_position(position_address(Keypair().pubkey(), SHORT), raw)
    assert position.direction == Direction.SHORT
    assert position.price == Decimal("116.325197")
    assert position.size_usd > position.collateral_usd > 0


def test_bytes_of_another_account_are_refused():
    with pytest.raises(AccountFormatError, match="não é um Custody"):
        decode_account("Custody", ACCOUNTS["Pool"])
    with pytest.raises(AccountFormatError, match="curto"):
        decode_account("Custody", ACCOUNTS["Custody"][:100])


def test_a_position_address_is_one_per_wallet_market_and_side():
    owner = Keypair().pubkey()
    assert position_address(owner, SHORT) == position_address(owner, SHORT)
    assert position_address(owner, SHORT) != position_address(owner, LONG)
    assert position_address(owner, SHORT) != position_address(Keypair().pubkey(), SHORT)


def _account(data: bytes | None) -> dict | None:
    return (
        None if data is None else {"data": [base64.b64encode(data).decode(), "base64"]}
    )


def _rpc(by_address: dict[str, bytes | None]) -> httpx.AsyncClient:
    """Um RPC falso: `getMultipleAccounts` das contas dadas."""

    def handle(request: httpx.Request) -> httpx.Response:
        addresses = json.loads(request.content)["params"][0]
        value = [_account(by_address.get(a)) for a in addresses]
        return httpx.Response(200, json={"result": {"value": value}})

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


async def test_the_reader_reads_the_custody_and_finds_no_position():
    custody = str(CUSTODIES[SOL_MINT])
    reader = JupiterPerpsReader("http://rpc.test", _rpc({custody: ACCOUNTS["Custody"]}))
    try:
        assert (await reader.custody(SOL_MINT)).decimals == 9
        assert await reader.position(Keypair().pubkey(), SHORT) is None
        with pytest.raises(PerpsReadError, match="não existe"):
            await reader.pool()
    finally:
        await reader.aclose()
    assert str(JLP_POOL) not in repr(reader) and "rpc.test" not in repr(reader)


async def test_a_failed_rpc_is_a_read_error_and_a_429_is_retried():
    calls = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:  # o RPC público em rajada
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(400)  # um erro que não se tenta de novo

    client = httpx.AsyncClient(transport=httpx.MockTransport(flaky))
    reader = JupiterPerpsReader("http://rpc.test", client)
    try:
        with pytest.raises(PerpsReadError, match="RPC falhou"):
            await reader.custody(SOL_MINT)
    finally:
        await reader.aclose()
    assert len(calls) == 2
