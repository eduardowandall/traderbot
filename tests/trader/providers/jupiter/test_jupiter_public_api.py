from unittest import mock

from trader.providers.jupiter.jupiter_data import (
    JupiterQuoteResponse,
    JupiterRoutePlan,
    JupiterSwapInfo,
)
from trader.providers.jupiter.jupiter_public_api import JupiterPublicAPI


class TestJupiterPublicAPI:
    def test_init(self):
        api_lite = JupiterPublicAPI(use_pro=False)
        assert api_lite.quote_base_url == "https://lite-api.jup.ag/swap/v1"
        assert api_lite.price_base_url == "https://api.jup.ag"
        assert api_lite.session.headers["Content-Type"] == "application/json"
        assert api_lite.logger is not None

        api_pro = JupiterPublicAPI(use_pro=True)
        assert api_pro.quote_base_url == "https://quote-api.jup.ag/v6"
        assert api_pro.price_base_url == "https://api.jup.ag"
        assert api_lite.session.headers["Content-Type"] == "application/json"
        assert api_lite.logger is not None

    class TestGetQuote:
        fake_request_get_quote = {
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
        }

        def _assert_response(self, response: JupiterQuoteResponse):
            assert response == JupiterQuoteResponse(
                input_mint="So11111111111111111111111111111111111111112",
                in_amount="1000000000",
                output_mint="EPjFWdd5Au...",
                out_amount="50000000",
                other_amount_threshold="49500000",
                swap_mode="ExactIn",
                slippage_bps=50,
                platform_fee=None,
                price_impact_pct="0.5",
                route_plan=[
                    JupiterRoutePlan(
                        swap_info=JupiterSwapInfo(
                            amm_key="FksffEqnBRixYGR791Qw2MgdU7zNCpHVFYBL4Fa4qVuH",
                            label="HumidiFi",
                            input_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                            output_mint="So11111111111111111111111111111111111111112",
                            in_amount="1000000000",
                            out_amount="7106793162",
                            fee_amount="0",
                            fee_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                        ),
                        percent=100,
                    )
                ],
                context_slot=123456789,
                time_taken=0.5,
            )

        @mock.patch.object(
            JupiterPublicAPI,
            "_make_public_request",
            return_value=fake_request_get_quote,
        )
        def test_get_quote(self, mock_make_request):
            api = JupiterPublicAPI(use_pro=False)
            response = api.get_quote(
                input_mint="So11111111111111111111111111111111111111112",
                output_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                amount=1000000000,
                slippage_bps=50,
            )
            self._assert_response(response)
            mock_make_request.assert_called_once_with(
                "https://lite-api.jup.ag/swap/v1",
                "GET",
                "/quote",
                params={
                    "inputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                    "outputMint": "So11111111111111111111111111111111111111112",
                    "amount": "1000000000",
                    "slippageBps": "50",
                },
            )

        @mock.patch.object(
            JupiterPublicAPI,
            "_make_public_request",
            return_value=fake_request_get_quote,
        )
        def test_only_direct_routes(self, mock_make_request):
            api = JupiterPublicAPI(use_pro=False)
            response = api.get_quote(
                input_mint="So11111111111111111111111111111111111111112",
                output_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                amount=1000000000,
                slippage_bps=50,
                only_direct_routes=True,
            )
            self._assert_response(response)
            mock_make_request.assert_called_once_with(
                "https://lite-api.jup.ag/swap/v1",
                "GET",
                "/quote",
                params={
                    "inputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                    "outputMint": "So11111111111111111111111111111111111111112",
                    "amount": "1000000000",
                    "slippageBps": "50",
                    "onlyDirectRoutes": "true",
                },
            )

        @mock.patch.object(
            JupiterPublicAPI,
            "_make_public_request",
            return_value=fake_request_get_quote,
        )
        def test_max_accounts(self, mock_make_request):
            api = JupiterPublicAPI(use_pro=False)
            response = api.get_quote(
                input_mint="So11111111111111111111111111111111111111112",
                output_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                amount=1000000000,
                slippage_bps=50,
                max_accounts=5,
            )
            self._assert_response(response)
            mock_make_request.assert_called_once_with(
                "https://lite-api.jup.ag/swap/v1",
                "GET",
                "/quote",
                params={
                    "inputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                    "outputMint": "So11111111111111111111111111111111111111112",
                    "amount": "1000000000",
                    "slippageBps": "50",
                    "maxAccounts": "5",
                },
            )
