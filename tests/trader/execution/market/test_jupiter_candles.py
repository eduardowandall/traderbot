from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock

from trader.execution.market import JupiterCandles
from trader.execution.market.jupiter.candles import candles_to_tickers
from trader.execution.market.jupiter.client import AsyncJupiterClient
from trader.shared.models import Interval

RAW = [
    {
        "time": 1_790_000_000,
        "open": "1",
        "high": "3",
        "low": "0.5",
        "close": "2",
        "volume": "10",
    }
]


def test_candles_to_tickers():
    [ticker] = candles_to_tickers(RAW)
    assert ticker.last == Decimal("2")


def test_float_prices_keep_their_short_decimal_form():
    [ticker] = candles_to_tickers([{**RAW[0], "close": 0.1}])
    assert ticker.last == Decimal("0.1")
    assert ticker.high == Decimal("3")
    assert ticker.timestamp == datetime.fromtimestamp(1_790_000_000)


async def test_jupiter_candles_need_only_the_public_client():
    client = AsyncMock(spec=AsyncJupiterClient)
    client.get_candles = AsyncMock(return_value=RAW)
    data = JupiterCandles(client)

    tickers = await data.get_candles("mint", Interval.HOUR_1, 5)
    await data.aclose()

    assert tickers[0].last == Decimal("2")
    client.get_candles.assert_awaited_once_with(
        "mint", interval=Interval.HOUR_1, candle_qty=5
    )
    client.aclose.assert_awaited_once()
