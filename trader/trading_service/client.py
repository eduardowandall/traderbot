"""`TradeClient`: o que uma estratégia pode fazer com o bucket dela.

É a única porta do lado da estratégia (bot, strategy-runner) para a execução.
Implementações: `LocalTradeClient` (mesmo processo) e, na fase 4, um cliente
por socket para o trade-runner. Um cliente fala com um único bucket.
"""

from typing import Protocol

from trader.trading_service.protocol import BucketSnapshot, OrderReply, OrderRequest


class TradeClient(Protocol):
    async def open(self) -> None:
        """Abre o bucket (restaura a posição do ledger, reconcilia)."""
        ...

    async def bucket(self) -> BucketSnapshot: ...

    async def submit(self, request: OrderRequest) -> OrderReply: ...

    async def aclose(self) -> None: ...
