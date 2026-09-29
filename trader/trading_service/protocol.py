"""O que trafega entre estratégia e execução (camada core: só dados).

Uma estratégia enxerga o bucket dela (`BucketSnapshot`), pede uma ordem
(`OrderRequest`) e recebe o desfecho (`OrderReply`). Não há exceções de
execução cruzando a fronteira: recusas e erros viram um `status`, então o
lado da estratégia nunca importa o gateway, a política ou o provider.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from trader.models import Order, OrderSide, Position


class BucketStatus(StrEnum):
    ACTIVE = "active"
    # a estratégia deve parar; quem executa fecha a posição (fase 4)
    RETIRING = "retiring"


class ReplyStatus(StrEnum):
    FILLED = "filled"  # executada
    DENIED = "denied"  # recusada pela política (ou intenção duplicada)
    REJECTED = "rejected"  # recusada antes de executar (saldo, impacto, ...)
    ERROR = "error"  # falha inesperada (rede, RPC); vale backoff


@dataclass(frozen=True)
class BucketSnapshot:
    bucket: str
    # quanto a estratégia pode gastar agora, na moeda de entrada (USDC/USDT)
    available_usd: Decimal
    position: Position | None
    realized_usd: Decimal
    budget_usd: Decimal | None = None  # None: sem teto (a carteira toda)
    status: BucketStatus = BucketStatus.ACTIVE
    pnl_summary: str = ""


@dataclass(frozen=True)
class OrderRequest:
    side: OrderSide
    quantity: Decimal  # do token comprado/vendido
    price: Decimal  # preço de mercado (USD) no momento do sinal
    rationale: str | None = None
    # quem pede pode fixar a chave (reenvio após reconexão não duplica)
    idempotency_key: str | None = None


@dataclass(frozen=True)
class SwapRequest:
    """Swap manual (qualquer par, sem posição), no bucket `manual`."""

    spend_mint: str
    receive_mint: str
    amount: Decimal  # do token gasto, em unidades de UI
    slippage_bps: int = 50
    rationale: str | None = None
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
