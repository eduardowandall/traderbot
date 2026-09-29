"""Fonte de preços e candles para estratégias e para o agente.

Camada market: só lê dados públicos. Não conhece chave, carteira nem RPC, então
um strategy-runner (ou o `market summary` do agente) pode usá-la num processo
sem `SOLANA_PRIVATE_KEY`. Quem executa swaps continua em
`trader.providers.jupiter.async_jupiter_svc`.
"""

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


class JupiterMarketData:
    """`MarketData` sobre o cliente HTTP/websocket da Jupiter (sem chave).

    O websocket de preço assina um único ativo por vez: use uma instância
    por mint acompanhado.
    """

    def __init__(self, client: AsyncJupiterClient | None = None):
        self.client = client or AsyncJupiterClient()

    async def get_price(self, mint: str) -> Decimal:
        return await self.client.get_price(mint)

    async def get_candles(
        self, mint: str, interval: Interval = Interval.SECOND_15, candle_qty: int = 100
    ) -> list[TickerData]:
        raw = await self.client.get_candles(
            mint, interval=interval, candle_qty=candle_qty
        )
        return candles_to_tickers(raw)

    async def aclose(self) -> None:
        await self.client.aclose()
