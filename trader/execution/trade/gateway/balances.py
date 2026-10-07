"""`WalletBalances`: o saldo da carteira, lido uma vez para todos os buckets.

Os buckets de um `TradeService` dividem a mesma carteira: com um cache por
conta, o bucket A não via o que o bucket B acabou de gastar. Aqui há um cache
só, invalidado a cada fill e com validade de `BALANCE_CACHE_TTL`.

A regra (A5): toda ordem lê a carteira de novo (`fresh`); o cache serve só o
que não é ordem (alocação, reconcile, snapshots). A leitura nova de um token
confere a conta dele pelo endereço: o índice por dono do RPC já voltou sem
uma conta que existia (A15).
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial

from solders.pubkey import Pubkey

from trader.execution.models.account_data import MintBalance
from trader.execution.models.venue import Venue
from trader.shared.logging_config import error_text
from trader.shared.models.mints import SOL_MINT

logger = logging.getLogger(__name__)

BALANCE_CACHE_TTL = timedelta(minutes=3)


class WalletBalances:
    def __init__(
        self,
        venue: Venue,
        clock: Callable[[], datetime] = partial(datetime.now, UTC),
    ):
        self.venue = venue
        self.clock = clock
        self._balances: list[MintBalance] | None = None
        self._read_at: datetime | None = None
        # sobe a cada invalidação: uma leitura que começou antes de um fill
        # (outro bucket, no `serve`) não pode guardar o saldo de antes dele
        self._generation = 0

    async def get(self, mint: Pubkey | str) -> Decimal:
        """Saldo de `mint` (0 se a carteira não tem)."""
        for balance in await self._all():
            if str(balance.mint) == str(mint):
                return balance.available
        return Decimal("0")

    async def fresh(self, mint: Pubkey | str) -> Decimal:
        """Saldo de `mint` lido agora: o que as ordens usam.

        Um token (não o SOL) é lido também pela conta dele, ao mesmo tempo, e
        vale a maior leitura; se a direta falha, fica a da carteira.
        """
        self.invalidate()
        if str(mint) == SOL_MINT:
            return await self.get(mint)
        listed, direct = await asyncio.gather(
            self.get(mint), self._direct(mint), return_exceptions=True
        )
        if isinstance(listed, BaseException):
            raise listed
        return _larger(mint, listed, direct)

    async def _direct(self, mint: Pubkey | str) -> Decimal:
        return await self.venue.token_balance(mint)

    async def _all(self) -> list[MintBalance]:
        if not self._stale():
            return self._balances or []
        generation = self._generation
        balances = await self.venue.balances()
        if generation == self._generation:
            self._balances, self._read_at = balances, self.clock()
        return balances

    def _stale(self) -> bool:
        if not self._balances or self._read_at is None:
            return True
        return self._read_at < self.clock() - BALANCE_CACHE_TTL

    def invalidate(self) -> None:
        """A carteira mudou (fill, ou outro bucket pode ter gastado): relê."""
        self._balances = None
        self._generation += 1


def _larger(mint, listed: Decimal, direct: Decimal | BaseException) -> Decimal:
    if isinstance(direct, BaseException):
        logger.warning(f"Leitura direta de {mint} falhou: {error_text(direct)}")
        return listed
    if direct != listed:
        logger.warning(
            f"Carteira mostrou {listed} de {mint}, a conta do token tem {direct}"
        )
    return max(listed, direct)
