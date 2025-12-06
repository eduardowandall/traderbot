import re
from decimal import Decimal
from unittest import mock

import pytest

from trader.models.public_data import TickerData
from trader.providers.jupiter.jupiter_adapter import JupiterPublicAPIAdapter
from trader.providers.jupiter.jupiter_data import JupiterQuoteResponse
from trader.providers.jupiter.jupiter_public_api import JupiterPublicAPI


class TestParseSymbol:
    def test_success(self):
        adapter = JupiterPublicAPIAdapter()
        symbol = "BONK-USDC"
        parsed = adapter._parse_symbol(symbol)
        assert parsed == (
            "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        )

    def test_invalid_format(self):
        adapter = JupiterPublicAPIAdapter()
        symbol = "BTCUSD"
        with pytest.raises(
            ValueError, match="Símbolo inválido: BTCUSD. Use formato 'TOKEN1-TOKEN2'"
        ):
            adapter._parse_symbol(symbol)

    def test_unknown_token(self):
        adapter = JupiterPublicAPIAdapter()
        symbol = "UNKNOWN-USDC"
        with pytest.raises(
            ValueError,
            match=re.escape(
                "Token não encontrado: UNKNOWN. Tokens disponíveis: ['SOL', 'USDC', 'USDT', 'BONK', 'JUP', 'PUMP', 'TURBO']"
            ),
        ):
            adapter._parse_symbol(symbol)


class TestGetTicker:
    @staticmethod
    def fake_get_quote(*args, **kwargs) -> JupiterQuoteResponse:
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
        return JupiterQuoteResponse.from_dict(data)

    @mock.patch.object(JupiterPublicAPI, "get_quote", return_value=fake_get_quote())
    def test_get_ticker(self, mock_get_quote):
        adapter = JupiterPublicAPIAdapter()
        symbol = "SOL-USDC"
        ticker = adapter.get_ticker(symbol)
        buy_price = Decimal("2")
        assert ticker == TickerData(
            buy=buy_price,
            timestamp=mock.ANY,
            high=buy_price,
            last=buy_price,
            low=buy_price,
            open=buy_price,
            pair=symbol,
            sell=Decimal("0.005"),
            vol=Decimal("0"),
            spread=Decimal("-99.7500"),
        )
        mock_get_quote.assert_has_calls(
            [
                mock.call(
                    input_mint="So11111111111111111111111111111111111111112",
                    output_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                    amount=1000000000,
                    slippage_bps=50,
                ),
                mock.call(
                    input_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                    output_mint="So11111111111111111111111111111111111111112",
                    amount=1000000000,
                    slippage_bps=50,
                ),
            ]
        )
