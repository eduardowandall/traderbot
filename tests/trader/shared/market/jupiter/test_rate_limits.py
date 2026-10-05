"""R4: backoff nos limites da API, inicialização resiliente, fallback sem martelar."""

from decimal import Decimal
from unittest import mock
from unittest.mock import AsyncMock

import httpx
import pytest

from trader.shared.market import JupiterMarketData
from trader.shared.market import data as market_data
from trader.shared.market.jupiter.async_jupiter_client import (
    AsyncJupiterClient,
    is_retryable,
)

SOL = "So11111111111111111111111111111111111111112"
URL = "https://x/price/v3"


def _response(status, body="{}", headers=None):
    return httpx.Response(
        status, text=body, headers=headers, request=httpx.Request("GET", URL)
    )


def _client(*responses):
    http = AsyncMock()
    http.get = AsyncMock(side_effect=list(responses))
    return AsyncJupiterClient(client=http, base_url="https://x"), http


class TestHttpRetry:
    async def test_a_429_waits_retry_after_then_succeeds(self):
        ok = f'{{"{SOL}": {{"usdPrice": 150}}}}'
        client, http = _client(
            _response(429, headers={"Retry-After": "3"}), _response(200, ok)
        )

        with mock.patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
            prices = await client.get_usd_prices([SOL])

        assert prices == {SOL: Decimal("150")}
        assert http.get.await_count == 2
        sleep.assert_awaited_once_with(3.0)

    async def test_a_client_error_is_not_retried(self):
        client, http = _client(_response(400))

        with pytest.raises(httpx.HTTPStatusError):
            await client.get_usd_prices([SOL])

        assert http.get.await_count == 1

    async def test_gives_up_after_four_attempts(self):
        client, http = _client(*[_response(503) for _ in range(4)])

        with (
            mock.patch("asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(httpx.HTTPStatusError),
        ):
            await client.get_usd_prices([SOL])

        assert http.get.await_count == 4

    def test_what_counts_as_retryable(self):
        assert is_retryable(httpx.ConnectTimeout("slow"))
        limited = httpx.HTTPStatusError(
            "x", request=httpx.Request("GET", URL), response=_response(429)
        )
        assert is_retryable(limited)
        assert not is_retryable(ValueError("x"))


class TestWebsocketCooldown:
    async def test_a_dead_websocket_is_skipped_and_rest_is_paced(self, monkeypatch):
        now = [100.0]
        client = AsyncMock(spec=AsyncJupiterClient)
        client.get_price = AsyncMock(side_effect=OSError("recusado"))
        client.get_usd_prices = AsyncMock(return_value={SOL: Decimal("150")})
        data = JupiterMarketData(client, monotonic=lambda: now[0])

        with mock.patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
            await data.get_price(SOL)  # falha -> Price API
            await data.get_price(SOL)  # dentro do cooldown: nem tenta o ws
            now[0] += market_data.WS_COOLDOWN_SECONDS + 1
            await data.get_price(SOL)  # cooldown acabou: tenta o ws de novo

        assert client.get_price.await_count == 2
        # a segunda consulta à API veio logo após a primeira: esperou
        assert sleep.await_args is not None
        assert sleep.await_args.args[0] == pytest.approx(market_data.REST_POLL_SECONDS)
