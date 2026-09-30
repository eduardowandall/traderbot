"""A decisão de um tick, a mesma no bot ao vivo e no backtest.

Preço + bucket -> estratégia -> talvez uma `OrderRequest`. Quem chama só
envia a ordem e trata a resposta (o bot com pausa e backoff, o backtest
contando trades e recusas).
"""

from decimal import Decimal

from trader.bot.config import Strategy
from trader.trading_service.protocol import BucketSnapshot, BucketStatus, OrderRequest


def bucket_done(snapshot: BucketSnapshot) -> bool:
    """Bucket encerrado (perda máxima) e já sem posição: nada mais a fazer.

    Encerrado com sobra aberta, a estratégia continua recebendo preços para
    poder vender (compras são recusadas pelo serviço).
    """
    return snapshot.status == BucketStatus.RETIRING and snapshot.position is None


def order_for(
    strategy: Strategy, price: Decimal, snapshot: BucketSnapshot
) -> OrderRequest | None:
    """A ordem que a estratégia pede neste preço, ou None."""
    signal = strategy.on_market_refresh(
        price, snapshot.available_usd, snapshot.position
    )
    if signal is None:
        return None
    return OrderRequest(signal.side, signal.quantity, price, signal.rationale)
