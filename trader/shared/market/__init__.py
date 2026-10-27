"""O feed da estratégia (sem rede): `MarketData`, `HubMarketData`, pares."""

from .feed import CandleHistory, CandleRange, CandleSource, HubMarketData, MarketData

__all__ = [
    "CandleHistory",
    "CandleRange",
    "CandleSource",
    "HubMarketData",
    "MarketData",
]
