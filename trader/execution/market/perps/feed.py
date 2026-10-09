"""`PerpsFeed`: o estado da Jupiter Perps que o processo lê, em cache (A12).

O par do `PriceHub` para as perps: um por `serve`, dado ao venue do modo (o
paper lê dele a taxa de empréstimo; o real, a custody e o preço do oráculo).
Cada ordem e cada varredura liam a custody e o oráculo de novo; agora:

- a custody (a curva de empréstimo, os oráculos) vale `CUSTODY_TTL_SECONDS`:
  a utilização muda devagar;
- o preço do oráculo (o feed agregado da Doves) vale `ORACLE_TTL_SECONDS`.

`fresh_price` recusa um oráculo parado: o preço que a Jupiter usa com mais de
`MAX_ORACLE_AGE_SECONDS` não abre posição (`StaleOracleError`, uma recusa:
nada foi enviado). Fechar não espera o oráculo: sair é sempre permitido.
"""

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal

from trader.execution.market.hub import MAX_AGE_SECONDS
from trader.execution.market.perps.reader import (
    CustodyState,
    JupiterPerpsReader,
    OraclePrice,
)
from trader.execution.models.errors import SwapRejectedError
from trader.shared.models.mints import SOLANA_MINTS

CUSTODY_TTL_SECONDS = 60.0
ORACLE_TTL_SECONDS = 1.0
# a do hub: mais velho que isso não abre posição
MAX_ORACLE_AGE_SECONDS = MAX_AGE_SECONDS


class StaleOracleError(SwapRejectedError):
    """O oráculo do venue está parado: nenhuma entrada com ele (§8.6)."""


class PerpsFeed:
    def __init__(
        self,
        reader: JupiterPerpsReader | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        # o leitor fica exposto: o venue real lê por ele as contas que mudam
        # a cada ordem (pedido, posição), que não têm cache
        self.reader = reader or JupiterPerpsReader()
        self.monotonic = monotonic
        self.now = now
        # mint -> (valor, quando foi lido, no relógio monotônico)
        self._custodies: dict[str, tuple[CustodyState, float]] = {}
        self._prices: dict[str, tuple[OraclePrice, float]] = {}

    def __repr__(self):
        return f"{self.__class__.__name__}({self.reader!r})"

    async def _cached[T](
        self,
        store: dict[str, tuple[T, float]],
        ttl: float,
        mint: str,
        read: Callable[[str], Awaitable[T]],
    ) -> T:
        cached = store.get(mint)
        if cached and self.monotonic() - cached[1] < ttl:
            return cached[0]
        value = await read(mint)
        store[mint] = (value, self.monotonic())
        return value

    async def custody(self, mint: str) -> CustodyState:
        return await self._cached(
            self._custodies, CUSTODY_TTL_SECONDS, mint, self.reader.custody
        )

    async def borrow_bps_hour(self, mint: str) -> Decimal:
        return (await self.custody(mint)).borrow_bps_hour

    async def oracle_price(self, mint: str) -> OraclePrice:
        return await self._cached(
            self._prices, ORACLE_TTL_SECONDS, mint, self.reader.oracle_price
        )

    async def fresh_price(self, mint: str) -> OraclePrice:
        """O preço do oráculo, se ele andou há pouco; senão `StaleOracleError`."""
        price = await self.oracle_price(mint)
        age = (self.now() - price.timestamp).total_seconds()
        if age > MAX_ORACLE_AGE_SECONDS:
            symbol = SOLANA_MINTS.symbol_of(mint)
            raise StaleOracleError(
                f"oráculo da Jupiter Perps parado há {age:.0f} s ({symbol}): "
                "nenhuma entrada"
            )
        return price

    async def aclose(self) -> None:
        await self.reader.aclose()
