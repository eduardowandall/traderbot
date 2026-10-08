import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx

from trader.execution.market import JupiterPriceOracle, usd_snapshot
from trader.execution.market import prices as prices_module
from trader.execution.market.jupiter.client import AsyncJupiterClient
from trader.shared.models import SOLANA_MINTS

SOL = SOLANA_MINTS.get_by_symbol("SOL").mint
JUP = SOLANA_MINTS.get_by_symbol("JUP").mint
USDC = SOLANA_MINTS.get_by_symbol("USDC").mint


def _client(prices=None):
    client = AsyncMock(spec=AsyncJupiterClient)
    client.get_usd_prices = AsyncMock(return_value=prices or {})
    return client


class TestClientPriceApi:
    async def test_parses_usd_prices_exactly_and_skips_missing(self):
        body = (
            f'{{"{SOL}": {{"usdPrice": 117.71724485916512, "blockId": 1}},'
            f' "{JUP}": {{"blockId": 2}}}}'
        )
        response = httpx.Response(
            200, text=body, request=httpx.Request("GET", "https://x/price/v3")
        )
        http = AsyncMock()
        http.get = AsyncMock(return_value=response)
        client = AsyncJupiterClient(client=http, base_url="https://x")

        prices = await client.get_usd_prices([SOL, JUP])

        assert prices == {SOL: Decimal("117.71724485916512")}
        http.get.assert_awaited_once_with(
            "https://x/price/v3", params={"ids": f"{SOL},{JUP}"}, timeout=10
        )

    async def test_no_mints_makes_no_request(self):
        http = AsyncMock()
        client = AsyncJupiterClient(client=http, base_url="https://x")

        assert await client.get_usd_prices([]) == {}
        http.get.assert_not_awaited()


class TestOracle:
    async def test_caches_until_the_ttl_expires(self):
        now = [0.0]
        client = _client({SOL: Decimal("150")})
        oracle = JupiterPriceOracle(client, ttl_seconds=10, monotonic=lambda: now[0])

        await oracle.usd_prices([SOL])
        now[0] = 9.9
        assert await oracle.usd_prices([SOL]) == {SOL: Decimal("150")}
        assert client.get_usd_prices.await_count == 1
        now[0] = 10.0
        await oracle.usd_prices([SOL])
        assert client.get_usd_prices.await_count == 2

    async def test_unknown_prices_are_left_out(self):
        oracle = JupiterPriceOracle(_client({SOL: Decimal("150")}))

        assert await oracle.usd_prices([SOL, JUP]) == {SOL: Decimal("150")}


class TestSnapshot:
    async def test_without_an_oracle_only_stables_are_known(self):
        assert await usd_snapshot(None, [SOL, USDC]) == {USDC: Decimal("1")}

    async def test_stables_are_exactly_one_dollar_without_a_request(self):
        client = _client({USDC: Decimal("0.9998")})

        snapshot = await usd_snapshot(JupiterPriceOracle(client), [USDC])

        assert snapshot == {USDC: Decimal("1")}
        client.get_usd_prices.assert_not_awaited()

    async def test_never_raises(self):
        oracle = JupiterPriceOracle(_client())
        oracle.client.get_usd_prices.side_effect = httpx.ConnectError("down")

        assert await usd_snapshot(oracle, [SOL]) == {}

    async def test_gives_up_after_the_timeout(self, monkeypatch):
        monkeypatch.setattr(prices_module, "SNAPSHOT_TIMEOUT_SECONDS", 0.01)

        class Slow:
            async def usd_prices(self, mints):
                await asyncio.sleep(1)
                return {SOL: Decimal("1")}

        assert await usd_snapshot(Slow(), [SOL]) == {}
