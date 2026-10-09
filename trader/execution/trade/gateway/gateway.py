"""Gateway de execução: o único caminho de uma intenção até um swap.

Fluxo de `submit`:
1. idempotência: se a chave já executou (ou pode ter executado), não repete;
2. política: avalia a intenção com o estado do ledger;
3. registra a intenção (recusada ou em execução) no ledger;
4. executa e registra o resultado; cada envio é gravado antes de acontecer.
   Falha após o envio vira UNCONFIRMED, que bloqueia novos trades do modo
   até ser resolvida (`resolve.py`, no início do `serve` e a cada varredura).

O circuit breaker conta só as falhas desde que este gateway foi criado:
reiniciar o processo rearma.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial

from trader.execution.models.errors import SwapRejectedError, TransactionSubmittedError
from trader.execution.models.execution import ExecutionResult
from trader.execution.models.intent import IntentRecord, TradeIntent, send_hook
from trader.execution.models.mode import RunningMode
from trader.execution.trade.ledger import AccountPnL, Ledger, ledger_path
from trader.execution.trade.ledger.events import FAILED_TX_FEE
from trader.execution.trade.policy import (
    Policy,
    evaluate,
    load_policy,
    send_refusals,
)
from trader.shared.models.costs import FailedTxFee, PnLResult
from trader.shared.models.order import Order
from trader.shared.models.position import Position

logger = logging.getLogger(__name__)


class PolicyDeniedError(Exception):
    def __init__(self, intent: TradeIntent, reasons: tuple[str, ...]):
        self.intent = intent
        self.reasons = reasons
        super().__init__("Trade recusado pela política: " + "; ".join(reasons))


class DuplicateIntentError(Exception):
    """A chave de idempotência já foi usada por uma intenção que moveu fundos."""

    def __init__(self, existing: IntentRecord):
        self.existing = existing
        super().__init__(
            f"Intenção duplicada: chave {existing.intent.idempotency_key!r} já "
            f"usada por {existing.intent.intent_id} ({existing.status})"
        )


@dataclass(frozen=True)
class AccountState:
    """O que o ledger sabe de uma conta: PnL realizado e a posição aberta."""

    realized_usd: Decimal
    totals: AccountPnL
    # a posição aberta, com as contagens desde a entrada (A20)
    open_position: Position | None = None
    entry_intent_id: str | None = None
    # para a estratégia retomar cooldown/rearme e a validade (`ttl_days`)
    opened_at: datetime | None = None  # abertura da conta (`bucket_opened`)
    last_exit_at: datetime | None = None  # última venda executada
    last_exit_price: Decimal | None = None  # preço dela (`below_last_exit`)

    @property
    def open_entry(self) -> Order | None:
        """A ordem de entrada da posição aberta."""
        return None if self.open_position is None else self.open_position.entry_order


class TradeGateway:
    """Único caminho até o ledger para quem executa (conta, serviço, CLI)."""

    def __init__(
        self,
        ledger: Ledger,
        policy: Policy,
        real_mode: bool,
    ):
        self.ledger = ledger
        self.policy = policy
        self.real_mode = real_mode
        # o circuit breaker só conta falhas deste processo
        self.started_at = datetime.now(UTC)

    @classmethod
    def for_mode(cls, mode: str, policy: Policy | None = None) -> TradeGateway:
        """Ledger do modo + política do modo (`None` carrega `policy.toml`)."""
        return cls(
            ledger=Ledger(ledger_path(mode)),
            policy=load_policy(mode=str(mode)) if policy is None else policy,
            real_mode=mode == RunningMode.REAL,
        )

    @classmethod
    def in_memory(cls, policy: Policy | None = None) -> TradeGateway:
        """Gateway de replay: ledger em memória, modo não real.

        O backtest passa pelo mesmo caminho do ao vivo (idempotência, ciclo
        da intenção, eventos), mas sem os limites da política
        (`Policy.unlimited()`, veja lá o porquê).
        """
        return cls(
            ledger=Ledger(),
            policy=Policy.unlimited() if policy is None else policy,
            real_mode=False,
        )

    def close(self) -> None:
        self.ledger.close()

    def __enter__(self) -> TradeGateway:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- estado e registros (sem passar pela política) ----------------------

    def restore(self, account_id: str) -> AccountState:
        """PnL e posição aberta da conta, para reconstruir após reinício."""
        position, entry_intent_id = self.ledger.open_position(account_id) or (
            None,
            None,
        )
        entry = position and position.entry_order
        opened_at, last_exit_at = self.ledger.account_times(account_id)
        totals = self.ledger.pnl_totals(account_id)
        return AccountState(
            realized_usd=totals.net_usd,
            totals=totals,
            open_position=position,
            entry_intent_id=entry_intent_id,
            opened_at=opened_at,
            last_exit_at=None if entry else last_exit_at,
            last_exit_price=None if entry else self.ledger.last_exit_price(account_id),
        )

    def open_positions(self, prefix: str = "") -> dict[str, Decimal]:
        """Tokens em posições spot abertas, por mint (`Ledger.open_positions`)."""
        return self.ledger.open_positions(prefix)

    def record_external(
        self, intent: TradeIntent, result: ExecutionResult, reason: str
    ) -> bool:
        """Uma perna que o venue fez sem nós (liquidação, A8); fora da política."""
        return self.ledger.record_external(intent, result, reason)

    def open_account(self, account_id: str) -> None:
        """Marca a abertura da conta (a primeira vez que um bucket abre)."""
        self.ledger.mark_account_opened(account_id)

    def record_fill(
        self,
        intent_id: str,
        order: Order,
        realized_usd: Decimal | None = None,
        pnl: PnLResult | None = None,
    ) -> None:
        """Grava a ordem (e o PnL, em vendas) de uma intenção já EXECUTED.

        Fica separado de `mark_executed` de propósito: os custos são buscados
        entre os dois, e a intenção precisa estar EXECUTED antes disso.
        """
        self.ledger.attach_order(intent_id, order, realized_usd, pnl)

    def send_refusals(self) -> tuple[str, ...]:
        """As travas da política para um envio que não é swap (A15)."""
        state = self.ledger.policy_state(failures_since=self.started_at)
        return send_refusals(self.policy, state, real_mode=self.real_mode)

    def add_event(self, type_: str, payload: dict) -> None:
        self.ledger.add_event(type_, payload)

    def record_failed_fee(self, intent: TradeIntent, fee: FailedTxFee) -> None:
        """Taxas de tentativas que falharam na rede: custo do bucket, sem trade.

        Um evento (não uma coluna da intenção): vale para intenções que
        terminaram EXECUTED, FAILED ou REJECTED, e `pnl_totals` o desconta do PnL.
        """
        self.ledger.add_event(
            FAILED_TX_FEE,
            {
                "account": intent.account,
                "intent_id": intent.intent_id,
                "signatures": list(fee.signatures),
                "fee_lamports": fee.lamports,
                "fee_usd": fee.usd,
            },
            intent_id=intent.intent_id,
        )

    async def submit(
        self,
        intent: TradeIntent,
        execute: Callable[[], Awaitable[ExecutionResult]],
    ) -> ExecutionResult:
        self._authorize(intent)
        result = await self._execute(intent, execute)
        try:
            marked = self.ledger.mark_executed(intent.intent_id, result)
        except Exception:
            # o swap aconteceu: a assinatura e os valores precisam sobreviver
            # (a intenção fica EXECUTING e bloqueia novos trades)
            logger.error(
                f"Swap executado mas não registrado ({intent.intent_id}): {result}"
            )
            raise
        if not marked:
            # a intenção já tinha um desfecho final: não vira trade aqui
            raise RuntimeError(
                f"Intenção {intent.intent_id} já finalizada; swap {result.signature} "
                "não registrado como executado"
            )
        return result

    def _authorize(self, intent: TradeIntent) -> None:
        """Idempotência + política; registra a intenção (ou a recusa)."""
        existing, decision = self.ledger.authorize(
            intent,
            lambda state: evaluate(
                intent, self.policy, state, real_mode=self.real_mode
            ),
            failures_since=self.started_at,
        )
        if existing is not None:
            raise DuplicateIntentError(existing)
        assert decision is not None  # sem existente, sempre há decisão
        if not decision.allowed:
            logger.warning(f"Intenção {intent.intent_id} recusada: {decision.reasons}")
            raise PolicyDeniedError(intent, decision.reasons)

    async def _execute(
        self,
        intent: TradeIntent,
        execute: Callable[[], Awaitable[ExecutionResult]],
    ) -> ExecutionResult:
        """Executa e registra falhas; o sucesso é registrado por `submit`.

        Durante a execução, cada envio é gravado antes de acontecer
        (`send_hook`): é o que permite resolver a intenção depois (A3).
        """
        token = send_hook.set(partial(self.ledger.record_send, intent.intent_id))
        try:
            return await execute()
        except TransactionSubmittedError as ex:
            self.ledger.mark_unconfirmed(intent.intent_id, str(ex), ex.signature)
            raise
        except SwapRejectedError as ex:
            # recusa antes do envio (impacto de preço, saldo simulado): nada
            # quebrou, então não conta para o circuit breaker
            self.ledger.mark_rejected(intent.intent_id, str(ex))
            raise
        except Exception as ex:
            # erros antes do envio (quote, simulação, RPC) ou falha na rede
            self.ledger.mark_failed(intent.intent_id, f"{type(ex).__name__}: {ex}")
            raise
        except BaseException as ex:
            # cancelamento (Ctrl+C) no meio da execução: não dá para saber se a
            # transação chegou a ser enviada, então bloqueia até o dono conferir
            self.ledger.mark_unconfirmed(
                intent.intent_id, f"interrompida: {type(ex).__name__}"
            )
            raise
        finally:
            send_hook.reset(token)
