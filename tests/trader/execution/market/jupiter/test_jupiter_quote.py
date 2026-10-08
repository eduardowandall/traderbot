from trader.execution.market.jupiter.quote import (
    JupiterQuoteResponse,
    JupiterRoutePlan,
    JupiterSwapInfo,
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
        assert swap_info.ammKey == "11111111111111111111111111111111"
        assert swap_info.label == "Raydium"
        assert swap_info.inputMint == "So11111111111111111111111111111111111111112"
        assert swap_info.feeAmount == "50000"


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
        assert route_plan.swapInfo.label == "Raydium"


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
        assert quote.inputMint == "So11111111111111111111111111111111111111112"
        assert quote.slippageBps == 50
        assert len(quote.routePlan) == 1
