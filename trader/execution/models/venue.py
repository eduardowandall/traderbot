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

`PerpVenue` (A8) é o local das perps: abre e fecha (ou reduz, A12) uma
posição por mercado e lado, põe colateral, guarda o stop dela e diz quais o
venue fechou sozinho. Os dois têm os métodos de depois da execução
(`PostTrade`), que é o que `fills.py` usa.
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
from trader.shared.models.direction import Direction
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


@dataclass(frozen=True)
class PerpSweep:
    """Uma leitura das posições abertas (A20): as que o venue fechou sozinho
    e as que seguem, como estão agora (a liquidação de agora)."""

    exits: list[Liquidation]
    held: dict[PerpTerms, PerpFill]


class PerpVenue(PostTrade, Protocol):
    """O local das perps (A8): uma posição por mercado e lado na carteira.

    Toda ação que envia algo recebe a `key` da intenção (o pedido sai dela,
    D8) e devolve um `ExecutionResult` para o gateway, inclusive as ordens que
    ficam no venue (o stop: `venue_order` é o endereço dele, A12).
    """

    async def open_perp(
        self, collateral_mint: str, terms: PerpTerms, collateral: Decimal, key: str
    ) -> ExecutionResult:
        """Posta `collateral` (UI do token de cotação) e abre a posição."""
        ...

    async def close_perp(
        self,
        collateral_mint: str,
        terms: PerpTerms,
        key: str,
        fraction: Decimal = Decimal(1),
    ) -> ExecutionResult:
        """Fecha a posição (ou `fraction` dela, A12); o colateral volta."""
        ...

    async def add_collateral(
        self, collateral_mint: str, terms: PerpTerms, collateral: Decimal, key: str
    ) -> ExecutionResult:
        """Colateral a mais na posição aberta (A12): o `perp` do resultado é a
        posição inteira depois (colateral e liquidação novos)."""
        ...

    async def check_fresh(self, terms: PerpTerms) -> None:
        """Levanta se o preço do venue para `terms` está parado: nenhuma
        entrada com ele (A20: antes de a intenção existir)."""
        ...

    async def place_stop(
        self, terms: PerpTerms, fill: PerpFill, key: str
    ) -> ExecutionResult:
        """A ordem de stop no venue (D7); `venue_order` é o endereço dela, ou
        None se o venue não precisa de envio (o paper guarda o nível)."""
        ...

    async def stop_left(self, terms: PerpTerms, order: str) -> bool:
        """Depois de fechar: a ordem de stop `order` continua no venue?"""
        ...

    async def cancel_stop(
        self, terms: PerpTerms, order: str, key: str
    ) -> ExecutionResult:
        """Cancela a ordem de stop `order` que sobrou (A12)."""
        ...

    async def sweep(self, open_: Sequence[PerpTerms]) -> PerpSweep:
        """As posições `open_` numa leitura só: as que o venue fechou sozinho
        (liquidação ou o stop dele), ao preço de agora, e as outras.

        Só informa: uma saída continua até `acknowledge`, depois que quem
        pergunta a registrou.
        """
        ...

    async def acknowledge(self, terms: PerpTerms) -> None:
        """A saída de `terms` está registrada: o venue pode esquecê-la."""
        ...

    async def has_position(self, terms: PerpTerms) -> bool:
        """O venue já tem uma posição neste mercado e lado (uma por carteira)?"""
        ...

    async def open_markets(self) -> list[tuple[str, Direction]]:
        """(mercado, lado) de cada posição aberta no venue, numa leitura só
        (a conferência contra o ledger, D10)."""
        ...

    async def resolve_send(
        self, sent: SentTx, terms: PerpTerms
    ) -> ExecutionResult | None:
        """O resultado de um envio que entrou na rede (A12, a resolução A3).

        None: o keeper ainda não decidiu. `SwapRejectedError`: recusado
        (nada mudou na posição).
        """
        ...

    async def aclose(self) -> None: ...
