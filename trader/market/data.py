"""Fonte de preços e candles para estratégias e para o agente.

Camada market: só lê dados públicos. Não conhece chave, carteira nem RPC, então
um strategy-runner (ou o `market summary` do agente) pode usá-la num processo
sem `SOLANA_PRIVATE_KEY`. Quem executa swaps continua em
`trader.providers.jupiter.async_jupiter_svc`.
"""

import asyncio
import logging
from decimal import Decimal
from typing import Protocol

from trader.models.public_data import Interval, TickerData
from trader.providers.jupiter.async_jupiter_client import AsyncJupiterClient
from trader.providers.jupiter.candles import candles_to_tickers


class MarketData(Protocol):
    async def get_price(self, mint: str) -> Decimal: ...

    async def get_candles(
        self, mint: str, interval: Interval, candle_qty: int
    ) -> list[TickerData]: ...

    async def aclose(self) -> None: ...


__all__ = ["JupiterMarketData", "MarketData", "candles_to_tickers"]

logger = logging.getLogger(__name__)

# tempo máximo esperando um preço do websocket antes de ir à Price API
PRICE_TIMEOUT_SECONDS = 30.0


def _is_timeout(ex: Exception) -> bool:
    return isinstance(ex, TimeoutError)


class JupiterMarketData:
    """`MarketData` sobre o cliente HTTP/websocket da Jupiter (sem chave).

    O websocket de preço assina um único ativo por vez: use uma instância
    por mint acompanhado.

    O websocket é um endpoint não documentado. Se ele falha ou fica mudo por
    `price_timeout` segundos, o preço vem da Price API V3 (documentada); a
    próxima chamada tenta o websocket de novo. Assim o bot segue recebendo
    ticks (e os stops seguem valendo) com o feed parado.
    """

    def __init__(
        self,
        client: AsyncJupiterClient | None = None,
        price_timeout: float = PRICE_TIMEOUT_SECONDS,
    ):
        self.client = client or AsyncJupiterClient()
        self.price_timeout = price_timeout

    async def get_price(self, mint: str) -> Decimal:
        try:
            return await asyncio.wait_for(
                self.client.get_price(mint), timeout=self.price_timeout
            )
        except Exception as ex:
            reason = "sem preço no websocket" if _is_timeout(ex) else repr(ex)
            logger.warning(f"{reason}; usando a Price API para {mint}")
            return await self._rest_price(mint)

    async def _rest_price(self, mint: str) -> Decimal:
        prices = await self.client.get_usd_prices([mint])
        if mint not in prices:
            raise LookupError(f"Price API sem preço para {mint}")
        return prices[mint]

    async def get_candles(
        self, mint: str, interval: Interval = Interval.SECOND_15, candle_qty: int = 100
    ) -> list[TickerData]:
        raw = await self.client.get_candles(
            mint, interval=interval, candle_qty=candle_qty
        )
        return candles_to_tickers(raw)

    async def aclose(self) -> None:
        await self.client.aclose()
