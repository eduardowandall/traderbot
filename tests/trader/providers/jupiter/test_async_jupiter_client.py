import base64
from unittest import mock

import httpx
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.solders import VersionedTransaction
from solders.system_program import transfer

from trader.providers.jupiter.async_jupiter_client import (
    DEFAULT_JUPITER_API_URL,
    AsyncJupiterClient,
)
from trader.providers.jupiter.jupiter_data import (
    JupiterQuoteResponse,
    JupiterRoutePlan,
    JupiterSwapInfo,
)


def _make_versioned_transaction() -> VersionedTransaction:
    keypair = Keypair()
    receiver = Pubkey.new_unique()
    ixs = [
        transfer(
            {"from_pubkey": keypair.pubkey(), "to_pubkey": receiver, "lamports": 1_000}
        )
    ]
    msg = Message.new_with_blockhash(ixs, keypair.pubkey(), Hash.default())
    return VersionedTransaction(msg, [keypair])


class TestAsyncJupiterClient:
    class TestDefaultBaseUrl:
        def test_defaults_to_api_jup_ag(self):
            assert DEFAULT_JUPITER_API_URL == "https://api.jup.ag"
            assert AsyncJupiterClient().base_url == "https://api.jup.ag"

    class TestApiKeyHeader:
        def test_no_header_without_api_key(self, monkeypatch):
            monkeypatch.delenv("JUPITER_API_KEY", raising=False)
            client = AsyncJupiterClient()
            assert "x-api-key" not in client.client.headers

        def test_header_from_constructor_arg(self):
            client = AsyncJupiterClient(api_key="my-secret-key")
            assert client.client.headers["x-api-key"] == "my-secret-key"

        def test_header_from_env(self, monkeypatch):
            monkeypatch.setenv("JUPITER_API_KEY", "env-secret-key")
            client = AsyncJupiterClient()
            assert client.client.headers["x-api-key"] == "env-secret-key"

        def test_injected_client_is_left_untouched(self):
            injected = httpx.AsyncClient()
            AsyncJupiterClient(client=injected, api_key="my-secret-key")
            assert "x-api-key" not in injected.headers

    class TestGetSwapTransaction:
        async def test_posts_to_configured_base_url(self):
            tx = _make_versioned_transaction()
            fake_response = httpx.Response(
                200,
                request=httpx.Request("POST", ""),
                json={"swapTransaction": base64.b64encode(bytes(tx)).decode()},
            )
            quote = JupiterQuoteResponse.single_route(
                input_mint="So11111111111111111111111111111111111111112",
                in_amount=1_000_000_000,
                output_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                out_amount=50_000_000,
            )
            pubkey = Pubkey.new_unique()

            with mock.patch.object(
                httpx.AsyncClient, "post", return_value=fake_response
            ) as mock_post:
                client = AsyncJupiterClient(
                    base_url="https://my-proxy.example.com", api_key="k"
                )
                result = await client.get_swap_transaction(quote, pubkey)

            assert result == tx
            mock_post.assert_called_once_with(
                "https://my-proxy.example.com/swap/v1/swap",
                json={
                    "quoteResponse": mock.ANY,
                    "userPublicKey": str(pubkey),
                },
            )

        async def test_defaults_to_api_jup_ag(self):
            tx = _make_versioned_transaction()
            fake_response = httpx.Response(
                200,
                request=httpx.Request("POST", ""),
                json={"swapTransaction": base64.b64encode(bytes(tx)).decode()},
            )
            quote = JupiterQuoteResponse.single_route(
                input_mint="So11111111111111111111111111111111111111112",
                in_amount=1_000_000_000,
                output_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                out_amount=50_000_000,
            )
            with mock.patch.object(
                httpx.AsyncClient, "post", return_value=fake_response
            ) as mock_post:
                client = AsyncJupiterClient()
                await client.get_swap_transaction(quote, Pubkey.new_unique())

            called_url = mock_post.call_args.args[0]
            assert called_url == "https://api.jup.ag/swap/v1/swap"

    class TestGetQuote:
        fake_request_get_quote = httpx.Response(
            200,
            request=httpx.Request("GET", ""),
            json={
                "inputMint": "So11111111111111111111111111111111111111112",
                "inAmount": "1000000000",
                "outputMint": "EPjFWdd5Au...",
                "outAmount": "50000000",
                "otherAmountThreshold": "49500000",
                "swapMode": "ExactIn",
                "slippageBps": 50,
                "platformFee": None,
                "priceImpactPct": "0.5",
                "routePlan": [
                    {
                        "swapInfo": {
                            "ammKey": "FksffEqnBRixYGR791Qw2MgdU7zNCpHVFYBL4Fa4qVuH",
                            "label": "HumidiFi",
                            "inputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                            "outputMint": "So11111111111111111111111111111111111111112",
                            "inAmount": "1000000000",
                            "outAmount": "7106793162",
                            "feeAmount": "0",
                            "feeMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                        },
                        "percent": 100,
                    }
                ],
                "contextSlot": 123456789,
                "timeTaken": 0.5,
            },
        )

        def _assert_response(self, response: JupiterQuoteResponse):
            assert response == JupiterQuoteResponse(
                inputMint="So11111111111111111111111111111111111111112",
                inAmount="1000000000",
                outputMint="EPjFWdd5Au...",
                outAmount="50000000",
                otherAmountThreshold="49500000",
                swapMode="ExactIn",
                slippageBps=50,
                platformFee=None,
                priceImpactPct="0.5",
                routePlan=[
                    JupiterRoutePlan(
                        swapInfo=JupiterSwapInfo(
                            ammKey="FksffEqnBRixYGR791Qw2MgdU7zNCpHVFYBL4Fa4qVuH",
                            label="HumidiFi",
                            inputMint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                            outputMint="So11111111111111111111111111111111111111112",
                            inAmount="1000000000",
                            outAmount="7106793162",
                            feeAmount="0",
                            feeMint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                        ),
                        percent=100,
                    )
                ],
                contextSlot=123456789,
                timeTaken=0.5,
            )

        @mock.patch.object(
            httpx.AsyncClient,
            "get",
            return_value=fake_request_get_quote,
        )
        async def test_get_quote(self, mock_make_request):
            client = AsyncJupiterClient()
            response = await client.get_quote(
                input_mint="So11111111111111111111111111111111111111112",
                output_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                amount=1000000000,
                slippage_bps=50,
            )
            self._assert_response(response)
            mock_make_request.assert_called_once_with(
                "https://api.jup.ag/swap/v1/quote",
                params={
                    "inputMint": "So11111111111111111111111111111111111111112",
                    "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                    "amount": "1000000000",
                    "slippageBps": "50",
                },
            )

        @mock.patch.object(
            httpx.AsyncClient,
            "get",
            return_value=fake_request_get_quote,
        )
        async def test_custom_base_url(self, mock_make_request):
            client = AsyncJupiterClient(base_url="https://my-proxy.example.com/")
            response = await client.get_quote(
                input_mint="So11111111111111111111111111111111111111112",
                output_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                amount=1000000000,
                slippage_bps=50,
            )
            self._assert_response(response)
            mock_make_request.assert_called_once_with(
                "https://my-proxy.example.com/swap/v1/quote",
                params={
                    "inputMint": "So11111111111111111111111111111111111111112",
                    "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                    "amount": "1000000000",
                    "slippageBps": "50",
                },
            )

        @mock.patch.object(
            httpx.AsyncClient,
            "get",
            return_value=fake_request_get_quote,
        )
        async def test_base_url_from_env(self, mock_make_request, monkeypatch):
            monkeypatch.setenv("JUPITER_API_URL", "https://from-env.example.com")
            client = AsyncJupiterClient()
            await client.get_quote(
                input_mint="So11111111111111111111111111111111111111112",
                output_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                amount=1000000000,
                slippage_bps=50,
            )
            called_url = mock_make_request.call_args.args[0]
            assert called_url == "https://from-env.example.com/swap/v1/quote"

        @mock.patch.object(
            httpx.AsyncClient,
            "get",
            return_value=fake_request_get_quote,
        )
        async def test_only_direct_routes(self, mock_make_request):
            client = AsyncJupiterClient()
            response = await client.get_quote(
                input_mint="So11111111111111111111111111111111111111112",
                output_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                amount=1000000000,
                slippage_bps=50,
                only_direct_routes=True,
            )
            self._assert_response(response)
            mock_make_request.assert_called_once_with(
                "https://api.jup.ag/swap/v1/quote",
                params={
                    "inputMint": "So11111111111111111111111111111111111111112",
                    "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                    "amount": "1000000000",
                    "slippageBps": "50",
                    "onlyDirectRoutes": "true",
                },
            )

        @mock.patch.object(
            httpx.AsyncClient,
            "get",
            return_value=fake_request_get_quote,
        )
        async def test_max_accounts(self, mock_make_request):
            client = AsyncJupiterClient()
            response = await client.get_quote(
                input_mint="So11111111111111111111111111111111111111112",
                output_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                amount=1000000000,
                slippage_bps=50,
                max_accounts=5,
            )
            self._assert_response(response)
            mock_make_request.assert_called_once_with(
                "https://api.jup.ag/swap/v1/quote",
                params={
                    "inputMint": "So11111111111111111111111111111111111111112",
                    "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                    "amount": "1000000000",
                    "slippageBps": "50",
                    "maxAccounts": "5",
                },
            )
