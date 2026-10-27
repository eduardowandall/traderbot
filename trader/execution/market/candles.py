"""`JupiterCandles`: os candles da Jupiter (`datapi`, sem chave).

O `CandleSource` do trade-runner (op `candles`, o aquecimento dos
`connect`s) e do backtest, e o `CandleRange` das janelas do passado do
backtest (A34, com cache: `history.py`). Os preços ao vivo vêm do `PriceHub`
(`hub.py`).
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from trader.execution.market.history import CHUNK_BARS, CandleCache, Chunk, chunks
from trader.execution.market.jupiter.candles import candles_to_tickers
from trader.execution.market.jupiter.client import AsyncJupiterClient
from trader.shared.indicators import to_utc
from trader.shared.models.public_data import Interval, TickerData

# pedidos de pedaços ao mesmo tempo: sem chave, rajadas dão 429
MAX_CONCURRENT_CHUNKS = 4


class JupiterCandles:
    def __init__(
        self,
        client: AsyncJupiterClient | None = None,
        cache: CandleCache | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.client = client or AsyncJupiterClient()
        self.cache = cache or CandleCache()
        self.now = now
        self._slots = asyncio.Semaphore(MAX_CONCURRENT_CHUNKS)

    async def get_candles(
        self, mint: str, interval: Interval = Interval.SECOND_15, candle_qty: int = 100
    ) -> list[TickerData]:
        raw = await self.client.get_candles(
            mint, interval=interval, candle_qty=candle_qty
        )
        return candles_to_tickers(raw)

    async def between(
        self, mint: str, interval: Interval, start: datetime, end: datetime
    ) -> list[TickerData]:
        """Os candles que abrem em `[start, end)`, em ordem (A34)."""
        parts = await asyncio.gather(
            *(self._chunk(mint, c) for c in chunks(interval, start, end))
        )
        # os pedaços vêm em ordem e cada um já ordenado e só com as barras dele
        lo, hi = to_utc(start), to_utc(end)
        return [c for part in parts for c in part if lo <= to_utc(c.timestamp) < hi]

    async def _chunk(self, mint: str, chunk: Chunk) -> list[TickerData]:
        cached = await asyncio.to_thread(self.cache.read, mint, chunk)
        if cached is not None:
            return cached
        async with self._slots:
            raw = await self.client.get_candles(
                mint, interval=chunk.interval, candle_qty=CHUNK_BARS, end=chunk.end
            )
        candles = sorted(
            (
                c
                for c in candles_to_tickers(raw)
                if chunk.start <= to_utc(c.timestamp) < chunk.end
            ),
            key=lambda c: c.timestamp,
        )
        # vazio: talvez a API ainda não tenha; pede de novo da próxima vez
        if candles and chunk.is_final(self.now()):
            self.cache.write(mint, chunk, candles)
        return candles

    async def aclose(self) -> None:
        await self.client.aclose()
