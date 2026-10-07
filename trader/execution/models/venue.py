"""`Venue`: onde as ordens de um modo executam (A7, D4).

A execução (conta, saldos, fills, resolução, serviço) só conhece este
protocolo; quem monta (`wiring`, o backtest) escolhe o local: hoje o
`SpotVenue` (`trader/execution/trade/venues/spot.py`), que embrulha o
provider da Jupiter, real ou paper. Perps (A8, A10) são outro `Venue`.

`open` gasta `spend` do input comprando o output; `close` vende `quantity`
do output de volta para o input (os mints são sempre os do bucket). Os
métodos de depois da execução nunca levantam: `fetch_costs` degrada para
custos desconhecidos, `fetch_failed_fees` conta a taxa base, `send_outcome`
devolve PENDING.
"""

from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Protocol

from solders.pubkey import Pubkey

from trader.execution.models.account_data import MintBalance
from trader.execution.models.execution import ExecutionResult
from trader.execution.models.intent import SentTx, TxOutcome
from trader.execution.models.rent import RentRefund
from trader.shared.models.costs import TradeCosts


class Venue(Protocol):
    @property
    def native_fee_reserve(self) -> Decimal:
        """SOL que fica na carteira para as taxas (0 no backtest)."""
        ...

    async def balances(self) -> list[MintBalance]: ...

    async def token_balance(self, mint: Pubkey | str) -> Decimal:
        """Um token lido pela conta dele, não pelo índice do dono (A15)."""
        ...

    async def open(
        self, input_mint: Pubkey, output_mint: Pubkey, spend: Decimal
    ) -> ExecutionResult: ...

    async def close(
        self, input_mint: Pubkey, output_mint: Pubkey, quantity: Decimal
    ) -> ExecutionResult: ...

    async def fetch_costs(self, result: ExecutionResult) -> TradeCosts: ...

    async def fetch_failed_fees(self, signatures: Sequence[str]) -> int: ...

    async def send_outcome(self, sent: SentTx) -> TxOutcome: ...

    async def close_token_account(
        self, mint: str, announce: Callable[[SentTx], None]
    ) -> RentRefund | None: ...

    async def aclose(self) -> None: ...
