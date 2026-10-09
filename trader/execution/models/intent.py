"""Intenções de trade: tudo que quer executar um swap passa por aqui.

As estratégias (pelo `SpotAccount` do bucket) criam um `TradeIntent`.
A política decide (`PolicyDecision`) e o gateway executa e registra o resultado
no ledger (`IntentRecord`).
"""

import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum, auto

from trader.execution.models.execution import ExecutionResult
from trader.execution.models.perp import PerpTerms
from trader.shared.models.mints import SOLANA_MINTS, Mint


class IntentSide(StrEnum):
    """O que uma intenção faz; o que cada lado significa fica aqui (A20)."""

    BUY = auto()  # abre/aumenta posição
    SELL = auto()  # fecha posição (saída: não sofre limites de orçamento)
    # a ordem de stop de uma perp colocada no venue (A12), e o cancelamento
    # de uma que sobrou (A20): nada é trocado, só as travas de envio valem
    STOP = auto()
    CANCEL = auto()
    # colateral a mais numa perp aberta (A12): gasta como uma compra, mas a
    # posição é a mesma (a entrada cresce)
    ADD = auto()

    @property
    def spends(self) -> bool:
        """Gasta do orçamento: os limites da política e a contagem de trades."""
        return self in (IntentSide.BUY, IntentSide.ADD)

    @property
    def moves_position(self) -> bool:
        """Move o dinheiro de uma posição (o resto: ordens no venue)."""
        return self not in (IntentSide.STOP, IntentSide.CANCEL)

    @property
    def is_trade(self) -> bool:
        """Abre ou fecha (a comparação com o replay, a ida e volta)."""
        return self in (IntentSide.BUY, IntentSide.SELL)


POSITION_SIDES = tuple(side for side in IntentSide if side.moves_position)
SPENDING_SIDES = tuple(side for side in IntentSide if side.spends)
TRADE_SIDES = tuple(side for side in IntentSide if side.is_trade)


class IntentStatus(StrEnum):
    DENIED = auto()  # recusada pela política; nada foi enviado
    EXECUTING = auto()  # aprovada, em execução
    EXECUTED = auto()  # swap confirmado
    FAILED = auto()  # falhou antes do envio; nada foi executado
    # recusada pelo provider antes do envio (impacto de preço, conferência da
    # quote, saldo simulado): nada quebrou, então fica fora do circuit breaker
    REJECTED = auto()
    # enviada à rede mas sem confirmação: pode ou não ter sido executada.
    # bloqueia novos trades do modo até ser resolvida (`gateway/resolve.py`)
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
    # perna de perp (A8): o mercado, o lado e a alavancagem; None no spot
    perp: PerpTerms | None = None
    idempotency_key: str = field(default_factory=lambda: uuid.uuid4().hex)
    intent_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime = field(default_factory=_now)

    def pair(self) -> tuple[Mint, Mint]:
        """(token de cotação, token negociado), pelo lado da intenção."""
        spend, receive = SOLANA_MINTS[self.spend_mint], SOLANA_MINTS[self.receive_mint]
        if self.side in SPENDING_SIDES:
            return spend, receive
        return receive, spend


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


class PerpSendKind(StrEnum):
    OPEN = auto()
    CLOSE = auto()
    STOP = auto()  # a ordem de stop no venue (D7)
    CANCEL = auto()  # cancelar a ordem de stop que sobrou
    ADD = auto()  # colateral a mais


@dataclass(frozen=True)
class PerpSend:
    """O que um envio de perp pediu ao venue (A12), para resolvê-lo depois.

    Real: o pedido (`PositionRequest`) e a posição, endereços que o keeper
    muda, e `before`: o tamanho (fechar) ou o colateral (pôr mais) da
    posição antes do envio, em USD, para saber se o keeper executou. Paper:
    o fill já calculado (`perp_to_dict`), aplicado na hora.
    """

    kind: PerpSendKind
    request: str | None = None
    position: str | None = None
    fill: dict | None = None
    before: str | None = None


@dataclass(frozen=True)
class SentTx:
    """Uma transação de swap prestes a ser enviada (A3).

    Gravada no ledger (`intent_sent`) **antes** do envio: depois de um
    processo morto no meio, é o que permite conferir na rede se ela entrou.
    Uma perp (A12) leva também o `perp`; os mints são os do bucket.
    """

    signature: str
    input_mint: str
    output_mint: str
    in_amount: int  # raw, conforme a quote
    out_amount: int
    # depois desta altura de bloco a transação não entra mais (None: paper)
    last_valid_block_height: int | None = None
    # paper: quando foi aplicada (epoch), para saber se saiu do registro
    sent_at: float | None = None
    perp: PerpSend | None = None

    @classmethod
    def from_payload(cls, payload: dict) -> SentTx:
        """O envio gravado (`intent_sent`); os de antes da A12 não têm `perp`."""
        perp = payload.get("perp")
        if perp is not None:
            perp = PerpSend(**{**perp, "kind": PerpSendKind(perp["kind"])})
        return cls(**{**payload, "perp": perp})

    def to_result(self) -> ExecutionResult:
        """O resultado do swap, se este envio entrou (valores da quote)."""
        return ExecutionResult(
            self.signature,
            self.input_mint,
            self.output_mint,
            self.in_amount,
            self.out_amount,
        )


class TxOutcome(StrEnum):
    """O que a rede (ou a carteira simulada) diz de uma `SentTx`."""

    LANDED = auto()  # confirmada com sucesso
    FAILED = auto()  # confirmada com erro: só a taxa foi paga
    EXPIRED = auto()  # não entrou e não entra mais: nada foi movido
    PENDING = auto()  # ainda não dá para saber


# quem grava os envios da intenção em execução: o gateway define durante o
# `execute()`; o executor chama `announce_send` logo antes de enviar
send_hook: ContextVar[Callable[[SentTx], None] | None] = ContextVar(
    "send_hook", default=None
)


def announce_send(sent: SentTx, required: bool = False) -> None:
    """Grava o envio antes de ele acontecer; se a gravação falha, levanta.

    `required` (on-chain): sem quem grave, levanta (antes do envio) em vez
    de enviar algo que nenhuma resolução encontraria depois.
    """
    hook = send_hook.get()
    if hook is not None:
        hook(sent)
    elif required:
        raise LookupError(f"envio {sent.signature} sem registro no ledger")
