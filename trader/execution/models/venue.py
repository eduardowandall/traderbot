"""`Venue`: onde as ordens de um modo executam (A7, D4).

A execução (conta, saldos, fills, resolução, serviço) só conhece este
protocolo; quem monta (`wiring`, o backtest) escolhe o local: hoje o
`SpotVenue` (`trader/execution/trade/venues/spot.py`), que embrulha o
provider da Jupiter, real ou paper. Perps são um `PerpVenue` (abaixo).

`open` gasta `spend` do input comprando o output; `close` vende `quantity`
do output de volta para o input (os mints são sempre os do bucket). Os
métodos de depois da execução nunca levantam: `fetch_costs` degrada para
custos desconhecidos, `fetch_failed_fees` conta a taxa base, `send_outcome`
devolve PENDING.

`PerpVenue` (A8) é o local das perps: abre e fecha uma posição por mercado e
lado, e diz quais o venue liquidou. Os dois têm os métodos de depois da
execução (`PostTrade`), que é o que `fills.py` usa.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from solders.pubkey import Pubkey

from trader.execution.models.execution import ExecutionResult
from trader.execution.models.intent import SentTx, TxOutcome
from trader.execution.models.perp import PerpTerms
from trader.execution.models.rent import RentRefund
from trader.shared.models.costs import TradeCosts
from trader.shared.models.perp import PerpFill

# o SOL que fica na carteira para as taxas, on-chain e em paper
DEFAULT_SOL_FEE_RESERVE = Decimal("0.02")


@dataclass
class MintBalance:
    available: Decimal
    mint: Pubkey


class PostTrade(Protocol):
    """Depois de executada: os custos e a taxa das tentativas que falharam."""

    async def fetch_costs(self, result: ExecutionResult) -> TradeCosts: ...

    async def fetch_failed_fees(self, signatures: Sequence[str]) -> int: ...


class Venue(PostTrade, Protocol):
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

    async def send_outcome(self, sent: SentTx) -> TxOutcome: ...

    async def close_token_account(
        self, mint: str, announce: Callable[[SentTx], None]
    ) -> RentRefund | None: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class Liquidation:
    """Uma posição que o venue fechou sozinho: a perna de saída. Liquidada
    (nada voltou) ou, no real (A11b), o stop do venue que disparou."""

    terms: PerpTerms
    result: ExecutionResult

    @property
    def liquidated(self) -> bool:
        return self.result.perp is None or self.result.perp.liquidated


class PerpVenue(PostTrade, Protocol):
    async def open_perp(
        self, collateral_mint: str, terms: PerpTerms, collateral: Decimal, key: str
    ) -> ExecutionResult:
        """Posta `collateral` (UI do token de cotação) e abre a posição.

        `key`: a chave de idempotência da intenção (o pedido sai dela, D8).
        """
        ...

    async def close_perp(
        self, collateral_mint: str, terms: PerpTerms, key: str
    ) -> ExecutionResult:
        """Fecha a posição inteira; o colateral que sobrou volta à carteira."""
        ...

    async def place_stop(
        self,
        terms: PerpTerms,
        fill: PerpFill,
        key: str,
        announce: Callable[[SentTx], None],
    ) -> str | None:
        """A ordem de stop no venue (D7); o endereço dela, ou None se o venue
        não guarda stops (o paper: o stop da spec basta)."""
        ...

    async def stop_left(self, terms: PerpTerms) -> str | None:
        """Depois de fechar: a ordem de stop que ficou no venue, se ficou."""
        ...

    async def liquidations(self, open_: Sequence[PerpTerms]) -> list[Liquidation]:
        """Das posições `open_`, as que o venue liquidou (ao preço de agora).

        Só informa: a posição continua até `acknowledge`, depois que quem
        pergunta registrou a liquidação.
        """
        ...

    async def acknowledge(self, terms: PerpTerms) -> None:
        """A liquidação de `terms` está registrada: o venue pode esquecê-la."""
        ...

    async def has_position(self, terms: PerpTerms) -> bool:
        """O venue já tem uma posição neste mercado e lado (uma por carteira)?"""
        ...

    async def aclose(self) -> None: ...
