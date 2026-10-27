"""O feed que a estratégia lê: `MarketData`, e `HubMarketData` sobre um hub.

Sem rede: quem fala com a Jupiter é o trade-runner (`trader.execution.market`).
Num `connect`, preços e candles vêm do trade-runner (ops `price` e
`candles`). Camada market.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from trader.shared.models.public_data import Interval, TickerData

TICK_SECONDS = 1.0  # um preço por segundo para cada bot


class CandleSource(Protocol):
    async def get_candles(
        self, mint: str, interval: Interval, candle_qty: int
    ) -> list[TickerData]: ...

    async def aclose(self) -> None: ...


class CandleRange(Protocol):
    """Candles de uma janela do passado (A34): os que abrem em `[start, end)`."""

    async def between(
        self, mint: str, interval: Interval, start: datetime, end: datetime
    ) -> list[TickerData]: ...


class CandleHistory(CandleSource, CandleRange, Protocol):
    """Os últimos N candles e as janelas do passado: a fonte do backtest."""


class MarketData(CandleSource, Protocol):
    async def get_price(self, mint: str) -> Decimal: ...


class Pacer:
    """Um passo a cada `interval` segundos: o ritmo de um bot (o hub responde
    na hora). `HubMarketData` e o tick remoto (A21) o usam."""

    def __init__(
        self,
        interval: float = TICK_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.interval = interval
        self.monotonic = monotonic
        self._next = 0.0

    async def wait(self) -> None:
        wait = self._next - self.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._next = self.monotonic() + self.interval


class HubMarketData:
    """`MarketData` com preços do hub do trade-runner.

    `price_of(mint)` é `RemoteTradeClient.price` num `connect`. Candles (só no
    aquecimento) vêm de `candles`: `RemoteCandles` (op `candles`).

    O hub responde na hora, então o feed dá o ritmo do bot: um preço a cada
    `interval` segundos (antes, o ritmo era o das mensagens do websocket).
    """

    def __init__(
        self,
        price_of: Callable[[str], Awaitable[Decimal]],
        candles: CandleSource,
        interval: float = TICK_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.price_of = price_of
        self.candles = candles
        self.pacer = Pacer(interval, monotonic)

    async def get_price(self, mint: str) -> Decimal:
        await self.pacer.wait()
        return await self.price_of(mint)

    async def get_candles(
        self, mint: str, interval: Interval, candle_qty: int
    ) -> list[TickerData]:
        return await self.candles.get_candles(mint, interval, candle_qty)

    async def aclose(self) -> None:
        await self.candles.aclose()
