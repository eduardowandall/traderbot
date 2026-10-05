"""O preço que a estratégia vê: o token no token de cotação do par.

Os feeds da Jupiter dão preços e candles em USD. Num par USDC/USDT isso já é o
preço em cotação; num par como JUP-SOL, a estratégia precisa de SOL por JUP:
`PairMarketData` divide o preço do token pelo do token de cotação, de dois
feeds (o websocket assina um ativo por conexão), e os candles barra a barra.
Camada market: só lê dados públicos.
"""

import asyncio
from collections.abc import Callable
from decimal import Decimal

from trader.shared.indicators import to_utc
from trader.shared.market.feed import MarketData
from trader.shared.models import SOLANA_MINTS
from trader.shared.models.public_data import Interval, TickerData


def market_for(symbol: str, factory: Callable[[], MarketData]) -> MarketData:
    """O feed da estratégia do par: o de sempre em USDC/USDT, senão a razão."""
    _, quote = SOLANA_MINTS.get_pair(symbol)
    if quote.is_usd_stable:
        return factory()
    return PairMarketData(factory(), factory(), quote.mint)


class PairMarketData:
    """`MarketData` de um par sem stablecoin: preço do token / preço da cotação."""

    def __init__(self, token_feed: MarketData, quote_feed: MarketData, quote: str):
        self.token_feed = token_feed
        self.quote_feed = quote_feed
        self.quote = quote

    async def get_price(self, mint: str) -> Decimal:
        token, quote = await asyncio.gather(
            self.token_feed.get_price(mint), self.quote_feed.get_price(self.quote)
        )
        if quote <= 0:
            raise ValueError(f"preço USD inválido para {self.quote}: {quote}")
        return token / quote

    async def get_candles(
        self, mint: str, interval: Interval, candle_qty: int
    ) -> list[TickerData]:
        token, quote = await asyncio.gather(
            self.token_feed.get_candles(mint, interval, candle_qty),
            self.quote_feed.get_candles(self.quote, interval, candle_qty),
        )
        return ratio_candles(token, quote)

    async def aclose(self) -> None:
        await asyncio.gather(self.token_feed.aclose(), self.quote_feed.aclose())


def ratio_candles(token: list[TickerData], quote: list[TickerData]) -> list[TickerData]:
    """Candles do token em cotação: as barras com a mesma abertura nas duas séries.

    Abertura e fechamento são exatos (razão dos dois); máxima e mínima são uma
    aproximação (os extremos do token sobre o fechamento da cotação), alargada
    para cobrir abertura e fechamento.
    """
    by_open = {to_utc(c.timestamp): c for c in quote if c.last}
    return [
        _ratio(c, by_open[to_utc(c.timestamp)])
        for c in sorted(token, key=lambda c: to_utc(c.timestamp))
        if to_utc(c.timestamp) in by_open
    ]


def _ratio(token: TickerData, quote: TickerData) -> TickerData:
    close = token.last / quote.last
    open_ = (token.open or token.last) / (quote.open or quote.last)
    high = (token.high or token.last) / quote.last
    low = (token.low or token.last) / quote.last
    return TickerData(
        timestamp=token.timestamp,
        high=max(high, open_, close),
        last=close,
        low=min(low, open_, close),
        open=open_,
    )
