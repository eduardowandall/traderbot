"""`JupiterCandles`: os candles da Jupiter (`datapi`, sem chave).

O `CandleSource` do trade-runner (op `candles`, o aquecimento dos
`connect`s) e do backtest. Os preços ao vivo vêm do `PriceHub` (`hub.py`).
"""

from trader.execution.market.jupiter.candles import candles_to_tickers
from trader.execution.market.jupiter.client import AsyncJupiterClient
from trader.shared.models.public_data import Interval, TickerData


class JupiterCandles:
    def __init__(self, client: AsyncJupiterClient | None = None):
        self.client = client or AsyncJupiterClient()

    async def get_candles(
        self, mint: str, interval: Interval = Interval.SECOND_15, candle_qty: int = 100
    ) -> list[TickerData]:
        raw = await self.client.get_candles(
            mint, interval=interval, candle_qty=candle_qty
        )
        return candles_to_tickers(raw)

    async def aclose(self) -> None:
        await self.client.aclose()
