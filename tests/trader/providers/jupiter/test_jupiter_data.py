from decimal import Decimal

from trader.providers.jupiter.jupiter_data import (
    JupiterPriceData,
    JupiterQuoteResponse,
    JupiterRoutePlan,
    JupiterSwapInfo,
    JupiterSwapResponse,
    JupiterTokenInfo,
)


class TestJupiterSwapInfo:
    def test_from_dict(self):
        data = {
            "ammKey": "11111111111111111111111111111111",
            "label": "Raydium",
            "inputMint": "So11111111111111111111111111111111111111112",
            "outputMint": "EPjFWdd5Au...",
            "inAmount": "1000000000",
            "outAmount": "50000000",
            "feeAmount": "50000",
            "feeMint": "So11111111111111111111111111111111111111112",
        }
        swap_info = JupiterSwapInfo.from_dict(data)
        assert swap_info.amm_key == "11111111111111111111111111111111"
        assert swap_info.label == "Raydium"
        assert swap_info.input_mint == "So11111111111111111111111111111111111111112"
        assert swap_info.fee_amount == "50000"


class TestJupiterRoutePlan:
    def test_from_dict(self):
        data = {
            "swapInfo": {
                "ammKey": "key",
                "label": "Raydium",
                "inputMint": "mint1",
                "outputMint": "mint2",
                "inAmount": "1000",
                "outAmount": "500",
                "feeAmount": "10",
                "feeMint": "mint1",
            },
            "percent": 100,
        }
        route_plan = JupiterRoutePlan.from_dict(data)
        assert route_plan.percent == 100
        assert route_plan.swap_info.label == "Raydium"


class TestJupiterQuoteResponse:
    def test_from_dict(self):
        data = {
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
                        "ammKey": "key",
                        "label": "Raydium",
                        "inputMint": "mint1",
                        "outputMint": "mint2",
                        "inAmount": "1000",
                        "outAmount": "500",
                        "feeAmount": "10",
                        "feeMint": "mint1",
                    },
                    "percent": 100,
                }
            ],
            "contextSlot": 123456789,
            "timeTaken": 0.5,
        }
        quote = JupiterQuoteResponse.from_dict(data)
        assert quote.input_mint == "So11111111111111111111111111111111111111112"
        assert quote.slippage_bps == 50
        assert len(quote.route_plan) == 1


class TestJupiterSwapResponse:
    def test_from_dict(self):
        data = {
            "swapTransaction": "base64_encoded_transaction",
            "lastValidBlockHeight": 123456789,
            "prioritizationFeeLamports": 1000,
        }
        swap_response = JupiterSwapResponse.from_dict(data)
        assert swap_response.swap_transaction == "base64_encoded_transaction"
        assert swap_response.last_valid_block_height == 123456789
        assert swap_response.prioritization_fee_lamports == 1000


class TestJupiterTokenInfo:
    def test_from_dict(self):
        data = {
            "address": "EPjFWdd5Au...",
            "chainId": 101,
            "decimals": 6,
            "name": "USD Coin",
            "symbol": "USDC",
            "logoURI": "https://example.com/usdc.png",
            "tags": ["stablecoin", "wrapped"],
            "extensions": {"coingeckoId": "usd-coin"},
        }
        token_info = JupiterTokenInfo.from_dict(data)
        assert token_info.symbol == "USDC"
        assert token_info.decimals == 6
        assert len(token_info.tags) == 2


class TestJupiterPriceData:
    def test_from_dict(self):
        data = {
            "id": "So11111111111111111111111111111111111111112",
            "mintSymbol": "SOL",
            "vsToken": "EPjFWdd5Au...",
            "vsTokenSymbol": "USDC",
            "price": "125.50",
        }
        price_data = JupiterPriceData.from_dict(data)
        assert price_data.mint_symbol == "SOL"
        assert price_data.price == Decimal("125.50")
        assert isinstance(price_data.price, Decimal)
