"""O que trafega entre estratégia e execução (camada core: só dados).

Uma estratégia enxerga o bucket dela (`BucketSnapshot`), pede uma ordem
(`OrderRequest`) e recebe o desfecho (`OrderReply`). Não há exceções de
execução cruzando a fronteira: recusas e erros viram um `status`, então o
lado da estratégia nunca importa o gateway, a política ou o provider.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from trader.shared.models import Order, OrderSide, Position


class BucketStatus(StrEnum):
    ACTIVE = "active"
    # a estratégia deve parar; quem executa vende o que sobrou
    RETIRING = "retiring"


class ReplyStatus(StrEnum):
    FILLED = "filled"  # executada
    DENIED = "denied"  # recusada pela política (ou intenção duplicada)
    REJECTED = "rejected"  # recusada antes de executar (saldo, impacto, ...)
    ERROR = "error"  # falha inesperada (rede, RPC); vale backoff


@dataclass(frozen=True)
class BucketSnapshot:
    bucket: str
    # quanto a estratégia pode gastar agora, no token de cotação (a entrada)
    available: Decimal
    position: Position | None
    realized_usd: Decimal
    # USD por unidade do token de cotação (1 em USDC/USDT); None: sem preço,
    # e então `available` é 0 (o orçamento em USD não pode ser conferido)
    quote_usd: Decimal | None
    budget_usd: Decimal | None = None  # None: sem teto (a carteira toda)
    status: BucketStatus = BucketStatus.ACTIVE
    pnl_summary: str = ""
    # do ledger: quando o bucket começou e a última saída (restart)
    opened_at: datetime | None = None
    last_exit_at: datetime | None = None
    last_exit_price: Decimal | None = None


@dataclass(frozen=True)
class OrderRequest:
    side: OrderSide
    quantity: Decimal  # do token comprado/vendido
    # preço de mercado no momento do sinal, no token de cotação (USD em
    # pares USDC/USDT)
    price: Decimal
    rationale: str | None = None
    # quem pede pode fixar a chave (reenvio após reconexão não duplica)
    idempotency_key: str | None = None


@dataclass(frozen=True)
class OrderReply:
    status: ReplyStatus
    order: Order | None = None
    reasons: tuple[str, ...] = ()
    error: str | None = None

    @property
    def filled(self) -> bool:
        return self.status == ReplyStatus.FILLED

    @classmethod
    def of_fill(cls, order: Order) -> OrderReply:
        return cls(ReplyStatus.FILLED, order=order)

    @classmethod
    def of_denial(cls, reasons: tuple[str, ...]) -> OrderReply:
        return cls(ReplyStatus.DENIED, reasons=reasons)

    @classmethod
    def of_rejection(cls, reason: str) -> OrderReply:
        return cls(ReplyStatus.REJECTED, reasons=(reason,))

    @classmethod
    def of_error(cls, error: str) -> OrderReply:
        return cls(ReplyStatus.ERROR, error=error)


class TradeServiceError(Exception):
    """Falha inesperada do serviço de trading, vista pelo lado da estratégia."""


class HelloRefusedError(TradeServiceError):
    """O trade-runner recusou a spec ou o token: reconectar não resolve.

    O bot para com o motivo (A11c F5): os termos, a política ou a carteira
    precisam mudar antes de um novo `connect`.
    """


# recusas de `hello` que passam sozinhas: a sessão antiga da spec que o
# trade-runner ainda não largou (uma reconexão logo depois de cair). O cliente
# as trata como conexão caída e tenta de novo; as outras são `HelloRefusedError`
HELLO_RETRY_KINDS = frozenset({"SpecConnectedError"})


class PriceUnavailableError(TradeServiceError):
    """O trade-runner não tem preço recente do mint: o bot não decide agora.

    Esperado (um reinício, a Price API fora), não um defeito do loop (F10).
    """


# `kind` de uma resposta recusada (o nome da exceção no trade-runner) -> a
# exceção do lado da estratégia; o que não está aqui vira `TradeServiceError`
REMOTE_ERRORS: dict[str, type[TradeServiceError]] = {
    "StalePriceError": PriceUnavailableError,  # `trader/execution/market/hub.py`
}
