"""`BucketAccount`: o que o `TradeService` usa da conta de um bucket (A7, D4).

Hoje só o `SpotAccount` (`trader/execution/trade/gateway/account.py`): um
par, uma posição por vez, tokens na carteira. A conta de perps (A8) guarda
colateral e uma posição no venue. BUY e SELL seguem querendo dizer entrar e
sair (D2).
"""

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from solders.pubkey import Pubkey

from trader.execution.models.book import PositionBook
from trader.shared.models import Order


class BucketAccount(Protocol):
    account_id: str
    input_mint: Pubkey  # o token de cotação: o que o bucket gasta
    output_mint: Pubkey  # o que ele compra
    book: PositionBook
    # do ledger (restore): a estratégia retoma prazo, cooldown e rearme
    opened_at: datetime | None
    last_exit_at: datetime | None
    last_exit_price: Decimal | None

    def restore_from_ledger(self) -> None: ...

    def committed(self) -> Decimal:
        """O que a posição aberta comprometeu, no token de cotação (0 sem posição)."""
        ...

    async def get_spendable_balance(self, mint: Pubkey) -> Decimal: ...

    async def buy(
        self,
        price: Decimal,
        quantity: Decimal,
        rationale: str | None = None,
        idempotency_key: str | None = None,
        limit: Decimal | None = None,
    ) -> Order: ...

    async def sell(
        self,
        price: Decimal,
        quantity: Decimal,
        rationale: str | None = None,
        idempotency_key: str | None = None,
    ) -> Order: ...
