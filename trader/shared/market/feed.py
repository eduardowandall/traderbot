"""O feed que a estratégia lê: `MarketData`, e `HubMarketData` sobre um hub.

Sem rede: quem fala com a Jupiter é o trade-runner (`trader.execution.market`).
No `run`, os preços vêm do `PriceHub` do processo; num `connect`, do
trade-runner (ops `price` e `candles`). Camada market.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Protocol

from trader.shared.models.public_data import Interval, TickerData

TICK_SECONDS = 1.0  # um preço por segundo para cada bot


class CandleSource(Protocol):
    async def get_candles(
        self, mint: str, interval: Interval, candle_qty: int
    ) -> list[TickerData]: ...

    async def aclose(self) -> None: ...


class MarketData(CandleSource, Protocol):
    async def get_price(self, mint: str) -> Decimal: ...


class HubMarketData:
    """`MarketData` com preços de um hub (local ou do trade-runner).

    `price_of(mint)` é `PriceHub.get` no `run`, ou `RemoteTradeClient.price`
    num `connect`. Candles (só no aquecimento) vêm de `candles`: a Jupiter no
    `run`, `RemoteCandles` (op `candles`) num `connect`.

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
        self.interval = interval
        self.monotonic = monotonic
        self._next = 0.0

    async def get_price(self, mint: str) -> Decimal:
        wait = self._next - self.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._next = self.monotonic() + self.interval
        return await self.price_of(mint)

    async def get_candles(
        self, mint: str, interval: Interval, candle_qty: int
    ) -> list[TickerData]:
        return await self.candles.get_candles(mint, interval, candle_qty)

    async def aclose(self) -> None:
        await self.candles.aclose()
