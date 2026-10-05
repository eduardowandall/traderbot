"""`WalletBalances`: o saldo da carteira, lido uma vez para todos os buckets.

Os buckets de um `TradeService` dividem a mesma carteira: com um cache por
conta, o bucket A não via o que o bucket B acabou de gastar. Aqui há um cache
só, invalidado a cada fill e com validade de `BALANCE_CACHE_TTL`.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial

from solders.pubkey import Pubkey

from trader.execution.models.account_data import MintBalance
from trader.execution.venues.jupiter.async_jupiter_svc import AsyncJupiterProvider

BALANCE_CACHE_TTL = timedelta(minutes=3)


class WalletBalances:
    def __init__(
        self,
        provider: AsyncJupiterProvider,
        clock: Callable[[], datetime] = partial(datetime.now, UTC),
    ):
        self.provider = provider
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

    async def _all(self) -> list[MintBalance]:
        if not self._stale():
            return self._balances or []
        generation = self._generation
        balances = await self.provider.get_account_balance()
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
