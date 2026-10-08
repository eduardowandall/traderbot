"""`SpotVenue`: o `Venue` dos swaps da Jupiter (A7).

Embrulha um `AsyncJupiterProvider` (real ou paper, ou o do replay) sem
mudar nada do que ele faz; `provider` fica exposto para inspeção e testes
(`spot_provider` em `tests/factories.py`).
"""

from collections.abc import Callable, Sequence
from decimal import Decimal

from solders.pubkey import Pubkey

from trader.execution.models.execution import ExecutionResult
from trader.execution.models.intent import SentTx, TxOutcome
from trader.execution.models.rent import RentRefund
from trader.execution.models.venue import MintBalance
from trader.execution.trade.venues.jupiter.provider import AsyncJupiterProvider
from trader.shared.models.costs import TradeCosts


class SpotVenue:
    def __init__(self, provider: AsyncJupiterProvider):
        self.provider = provider

    def __repr__(self):
        return f"{self.__class__.__name__}({self.provider!r})"

    @property
    def native_fee_reserve(self) -> Decimal:
        return self.provider.native_fee_reserve

    async def balances(self) -> list[MintBalance]:
        return await self.provider.get_account_balance()

    async def token_balance(self, mint: Pubkey | str) -> Decimal:
        return await self.provider.token_balance(mint)

    async def open(
        self, input_mint: Pubkey, output_mint: Pubkey, spend: Decimal
    ) -> ExecutionResult:
        return await self.provider.buy(input_mint, output_mint, spend)

    async def close(
        self, input_mint: Pubkey, output_mint: Pubkey, quantity: Decimal
    ) -> ExecutionResult:
        return await self.provider.sell(input_mint, output_mint, quantity=quantity)

    async def fetch_costs(self, result: ExecutionResult) -> TradeCosts:
        return await self.provider.fetch_swap_costs(result)

    async def fetch_failed_fees(self, signatures: Sequence[str]) -> int:
        return await self.provider.fetch_failed_fees(signatures)

    async def send_outcome(self, sent: SentTx) -> TxOutcome:
        return await self.provider.send_outcome(sent)

    async def close_token_account(
        self, mint: str, announce: Callable[[SentTx], None]
    ) -> RentRefund | None:
        return await self.provider.close_token_account(mint, announce)

    async def aclose(self) -> None:
        await self.provider.aclose()
