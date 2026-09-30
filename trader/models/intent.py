"""Intenções de trade: tudo que quer executar um swap passa por aqui.

As estratégias (pelo `AsyncAccount` do bucket) criam um `TradeIntent`.
A política decide (`PolicyDecision`) e o gateway executa e registra o resultado
no ledger (`IntentRecord`).
"""

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum, auto


class IntentSide(StrEnum):
    BUY = auto()  # abre/aumenta posição
    SELL = auto()  # fecha posição (saída: não sofre limites de orçamento)


class IntentStatus(StrEnum):
    DENIED = auto()  # recusada pela política; nada foi enviado
    EXECUTING = auto()  # aprovada, em execução
    EXECUTED = auto()  # swap confirmado
    FAILED = auto()  # falhou antes do envio; nada foi executado
    # enviada à rede mas sem confirmação: pode ou não ter sido executada.
    # bloqueia novos trades do modo até o dono mover ou apagar o ledger.
    UNCONFIRMED = auto()


# estados em que a intenção pode ter movido fundos (ainda sem desfecho)
ACTIVE_STATUSES = (IntentStatus.EXECUTING, IntentStatus.UNCONFIRMED)
# estados que movimentaram (ou podem ter movimentado) fundos
MOVED_FUNDS_STATUSES = (IntentStatus.EXECUTED, *ACTIVE_STATUSES)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class TradeIntent:
    """Pedido para gastar `spend_amount` de `spend_mint` recebendo `receive_mint`."""

    source: str  # quem pediu: estratégia, "cli", id do agente
    account: str  # conta lógica (modo + bucket), ex: "paper:strategy:ab12"
    side: IntentSide
    spend_mint: str
    receive_mint: str
    spend_amount: Decimal  # unidades UI do spend_mint
    # valor estimado em USD; None quando não há como estimar
    notional_usd: Decimal | None = None
    # preço de mercado (USD) no momento do sinal e quantidade pedida do token
    price: Decimal | None = None
    quantity: Decimal | None = None
    rationale: str | None = None
    # venda: já se sabe antes de executar que ela fecha a posição (limitada ao
    # saldo, ou a posição toda)? O restore usa isso se a ordem não foi gravada
    closes_position: bool | None = None
    idempotency_key: str = field(default_factory=lambda: uuid.uuid4().hex)
    intent_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime = field(default_factory=_now)


def with_idempotency_key(intent: TradeIntent, key: str | None) -> TradeIntent:
    """A intenção com a chave dada; sem chave, fica a aleatória padrão.

    Único lugar que decide o padrão da chave (quem cria intenções não
    repete `uuid4().hex`).
    """
    return replace(intent, idempotency_key=key) if key else intent


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reasons: tuple[str, ...] = ()
    policy_version: str = "defaults"


@dataclass
class IntentRecord:
    """Linha do ledger para uma intenção."""

    intent: TradeIntent
    status: IntentStatus
    decision_reasons: tuple[str, ...]
    policy_version: str
    updated_at: datetime
    signature: str | None = None
    in_amount: int | None = None  # raw, conforme a quote
    out_amount: int | None = None
    error: str | None = None
    # ordem resultante (json) e PnL realizado em USD (vendas)
    order_json: str | None = None
    realized_pnl_usd: Decimal | None = None
    # custos pagos em SOL e PnL nativo (token de cotação) da posição fechada
    fee_lamports: int | None = None
    rent_lamports: int | None = None
    other_lamports: int | None = None
    costs_source: str | None = None
    quote_mint: str | None = None
    net_pnl_quote: Decimal | None = None
    # recusas idênticas seguidas somadas nesta linha (além da primeira)
    repeat_count: int = 0
