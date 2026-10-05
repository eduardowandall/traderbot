"""O hub de preços: um feed para muitas estratégias.

`PriceHub` guarda o último `(preço, quando)` de cada mint pedido. Duas fontes:

- **um websocket** (`trench-stream.jup.ag`, não documentado) assinando todos
  os mints numa conexão só; um mint novo reabre a assinatura com a lista nova;
- **a Price API V3** (documentada), numa chamada só a cada `poll_seconds`,
  para os mints que o websocket não atualizou há `rest_after` segundos. Um
  token parado (o websocket só manda quando ele negocia) fica fresco sem que
  ninguém espere 30s por ele.

`price(mint)` nunca espera o websocket: devolve o último valor, ou levanta
`StalePriceError` se ele tem mais de `max_age` segundos (nenhuma estratégia
decide com dado velho). O trade-runner (`serve`) roda um hub e o serve aos
strategy-runners (op `price`); o `run` roda o seu no próprio processo.

O hub também é o `PriceOracle` do processo (`usd_prices`): o serviço, a
varredura, o relatório diário e a conferência das quotes leem dele, então só
ele chama a Price API. Camada market: só dados públicos.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Collection
from contextlib import aclosing
from dataclasses import dataclass
from decimal import Decimal

import websockets

from trader.shared.market.data import MarketData
from trader.shared.market.jupiter.async_jupiter_client import AsyncJupiterClient
from trader.shared.models.public_data import Interval, TickerData

logger = logging.getLogger(__name__)

WS_URL = "wss://trench-stream.jup.ag/ws"
POLL_SECONDS = 2.0  # o limite da Price API sem chave
REST_AFTER_SECONDS = 5.0  # sem notícia do websocket há isso: Price API
MAX_AGE_SECONDS = 30.0  # mais velho que isso não serve
RECONNECT_SECONDS = 5.0
TICK_SECONDS = 1.0  # um preço por segundo para cada bot

PriceStream = Callable[[Collection[str]], AsyncGenerator[tuple[str, Decimal]]]


class StalePriceError(LookupError):
    """Sem preço recente do mint: a estratégia não decide com ele."""


async def websocket_prices(
    mints: Collection[str],
) -> AsyncGenerator[tuple[str, Decimal]]:
    """`(mint, preço USD)` de cada mensagem do websocket, para os mints dados."""
    wanted = set(mints)
    async with websockets.connect(
        WS_URL, additional_headers={"Origin": "https://jup.ag"}, compression="deflate"
    ) as ws:
        await ws.send(
            json.dumps({"type": "subscribe:prices", "assets": sorted(wanted)})
        )
        async for message in ws:
            for item in json.loads(message, parse_float=Decimal).get("data") or []:
                if item.get("assetId") in wanted and "price" in item:
                    yield item["assetId"], Decimal(item["price"])


@dataclass(frozen=True)
class _Point:
    price: Decimal
    at: float  # relógio monotônico


class PriceHub:
    def __init__(
        self,
        client: AsyncJupiterClient | None = None,
        stream: PriceStream | None = websocket_prices,
        monotonic: Callable[[], float] = time.monotonic,
        poll_seconds: float = POLL_SECONDS,
        rest_after: float = REST_AFTER_SECONDS,
        max_age: float = MAX_AGE_SECONDS,
    ):
        self.client = client or AsyncJupiterClient()
        self.stream = stream  # None: só a Price API
        self.monotonic = monotonic
        self.poll_seconds = poll_seconds
        self.rest_after = rest_after
        self.max_age = max_age
        self._points: dict[str, _Point] = {}
        self._mints: set[str] = set()
        self._changed = asyncio.Event()

    # --- quem pede preço ---------------------------------------------------------

    async def price(self, mint: str) -> tuple[Decimal, float]:
        """(preço USD, idade em segundos); `StalePriceError` se velho ou ausente."""
        await self._watch([mint])
        point = self._points.get(mint)
        if point is None:
            raise StalePriceError(f"sem preço de {mint}")
        age = self.monotonic() - point.at
        if age > self.max_age:
            raise StalePriceError(f"preço de {mint} tem {age:.0f}s")
        return point.price, age

    async def get(self, mint: str) -> Decimal:
        return (await self.price(mint))[0]

    async def usd_prices(self, mints: Collection[str]) -> dict[str, Decimal]:
        """`PriceOracle`: os preços com menos de `max_age` segundos.

        Ao contrário de `price`, um mint velho tem mais uma chance, pela API
        agora (o hub pode não estar rodando ainda, ex: na reconciliação de
        antes do bot); o que segue sem preço fica de fora. Com o hub rodando,
        nada fica velho e nenhuma chamada sai daqui.
        """
        new = await self._watch(mints)
        stale = [m for m in set(mints) - new if self._age(m) > self.max_age]
        if stale:
            await self.poll(stale)
        return {m: self._points[m].price for m in mints if self._age(m) <= self.max_age}

    async def _watch(self, mints: Collection[str]) -> set[str]:
        """Passa a acompanhar os mints novos (o 1º preço vem já, pela API)."""
        new = set(mints) - self._mints
        if new:
            self._mints |= new
            self._changed.set()  # o websocket reassina com a lista nova
            await self.poll(new)
        return new

    # --- as fontes ------------------------------------------------------------------

    async def run(self) -> None:
        """As duas fontes, até ser cancelado.

        Cancelado, espera as fontes terminarem (o websocket fecha com um
        handshake) e só então fecha o cliente HTTP.
        """
        sources = [
            asyncio.ensure_future(self._poll_forever()),
            asyncio.ensure_future(self._stream_forever()),
        ]
        try:
            # `wait`, não `gather`: cancelado, ele não cancela as fontes; o
            # `finally` cancela uma vez só e espera (um segundo cancelamento
            # interromperia o fechamento do websocket)
            done, _ = await asyncio.wait(sources, return_when=asyncio.FIRST_EXCEPTION)
            for source in done:
                source.result()
        finally:
            for source in sources:
                source.cancel()
            await asyncio.gather(*sources, return_exceptions=True)
            await self.client.aclose()

    def _set(self, mint: str, price: Decimal) -> None:
        self._points[mint] = _Point(price, self.monotonic())

    def _age(self, mint: str) -> float:
        point = self._points.get(mint)
        return float("inf") if point is None else self.monotonic() - point.at

    async def poll(self, mints: Collection[str]) -> None:
        """Uma chamada à Price API para os mints dados; falha só avisa."""
        try:
            prices = await self.client.get_usd_prices(sorted(mints))
        except Exception as ex:
            logger.warning(f"Price API sem resposta para {sorted(mints)}: {ex}")
            return
        for mint, price in prices.items():
            self._set(mint, price)

    async def poll_stale(self) -> None:
        stale = [m for m in self._mints if self._age(m) > self.rest_after]
        if stale:
            await self.poll(stale)

    async def _poll_forever(self) -> None:
        while True:
            await asyncio.sleep(self.poll_seconds)
            await self.poll_stale()

    async def _stream_forever(self) -> None:
        if self.stream is None:
            return
        while True:
            if not self._mints:
                await self._changed.wait()
            self._changed.clear()
            try:
                await self._stream_until_changed(set(self._mints))
            except Exception as ex:
                logger.warning(f"Websocket de preços caiu ({ex}); a API cobre")
                await asyncio.sleep(RECONNECT_SECONDS)

    async def _stream_until_changed(self, mints: set[str]) -> None:
        """Lê o websocket até um mint novo pedir outra assinatura."""
        reading = asyncio.ensure_future(self._consume(mints))
        changed = asyncio.ensure_future(self._changed.wait())
        try:
            done, _ = await asyncio.wait(
                {reading, changed}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (reading, changed):
                task.cancel()
            await asyncio.gather(reading, changed, return_exceptions=True)
        if reading in done:
            reading.result()  # um erro do websocket sobe para reconectar

    async def _consume(self, mints: set[str]) -> None:
        assert self.stream is not None
        # cancelado (mint novo, Ctrl+C), o gerador fecha e o websocket também
        async with aclosing(self.stream(mints)) as prices:
            async for mint, price in prices:
                self._set(mint, price)
        raise ConnectionError("o websocket fechou")


class HubMarketData:
    """`MarketData` com preços de um hub (local ou do trade-runner).

    `price_of(mint)` é `PriceHub.get` no `run`, ou `RemoteTradeClient.price`
    num `connect`. Candles (só no aquecimento) vêm de `candles`.

    O hub responde na hora, então o feed dá o ritmo do bot: um preço a cada
    `interval` segundos (antes, o ritmo era o das mensagens do websocket).
    """

    def __init__(
        self,
        price_of: Callable[[str], Awaitable[Decimal]],
        candles: MarketData,
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
