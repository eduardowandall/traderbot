"""Preços em USD de qualquer token do registro (Price API V3 da Jupiter).

É o que dá valor em USD a trades cujo token gasto não é stablecoin e a
custos em SOL de pares sem SOL. Camada market: só lê dados públicos.

Quem executa tira um retrato dos preços **antes** do trade
(`usd_snapshot`), porque nada depois de EXECUTED pode falhar; e o retrato
nunca levanta: sem preço, o valor só fica desconhecido.

USDC/USDT valem exatamente 1 USD em todo o código (o valor de um trade pago
em stablecoin é o valor gasto). A regra mora aqui, em `usd_snapshot` e
`price_fn`, com ou sem oráculo; os oráculos só devolvem preços de mercado.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Collection
from decimal import Decimal
from typing import Protocol

from trader.shared.models import SOLANA_MINTS

logger = logging.getLogger(__name__)

ONE = Decimal("1")
DEFAULT_TTL_SECONDS = 10.0
SNAPSHOT_TIMEOUT_SECONDS = 5.0


class PriceOracle(Protocol):
    async def usd_prices(self, mints: Collection[str]) -> dict[str, Decimal]:
        """USD por unidade de cada mint; mints sem preço ficam de fora."""
        ...


def _is_stable(mint: str) -> bool:
    known = SOLANA_MINTS.get(mint)
    return known is not None and known.is_usd_stable


class JupiterPriceOracle:
    """Price API V3 com cache curto por mint (quem não tem um `PriceHub`)."""

    def __init__(
        self,
        client,  # AsyncJupiterClient (get_usd_prices)
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.client = client
        self.ttl_seconds = ttl_seconds
        self.monotonic = monotonic
        self._cache: dict[str, tuple[float, Decimal]] = {}

    async def usd_prices(self, mints: Collection[str]) -> dict[str, Decimal]:
        now = self.monotonic()
        prices: dict[str, Decimal] = {}
        missing = []
        for mint in set(mints):
            cached = self._cache.get(mint)
            if cached and now - cached[0] < self.ttl_seconds:
                prices[mint] = cached[1]
            else:
                missing.append(mint)
        if missing:
            fetched = await self.client.get_usd_prices(sorted(missing))
            for mint, price in fetched.items():
                self._cache[mint] = (now, price)
            prices |= fetched
        return prices


# `price_of(mint)`: o preço USD de um mint, ou None
PriceOf = Callable[[str], Awaitable[Decimal | None]]


def price_fn(oracle: PriceOracle | None) -> PriceOf:
    """`price_of(mint)`: o preço USD de um mint, ou None (sem preço)."""

    async def price_of(mint: str) -> Decimal | None:
        return (await usd_snapshot(oracle, [mint])).get(mint)

    return price_of


async def usd_snapshot(
    oracle: PriceOracle | None, mints: Collection[str]
) -> dict[str, Decimal]:
    """Preços USD (stablecoins a 1); nunca levanta (falha = desconhecidos)."""
    prices = {mint: ONE for mint in mints if _is_stable(mint)}
    rest = set(mints) - set(prices)
    if oracle is None or not rest:
        return prices
    try:
        return prices | await asyncio.wait_for(
            oracle.usd_prices(rest), timeout=SNAPSHOT_TIMEOUT_SECONDS
        )
    except Exception as ex:
        logger.warning(f"Sem preços em USD para {sorted(rest)}: {ex}")
        return prices
