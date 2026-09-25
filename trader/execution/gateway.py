"""Gateway de execução: o único caminho de uma intenção até um swap.

Fluxo de `submit`:
1. idempotência: se a chave já executou (ou pode ter executado), não repete;
2. política: avalia a intenção com o estado do ledger e o kill switch;
3. registra a intenção (recusada ou em execução) no ledger;
4. executa e registra o resultado. Falha após o envio vira UNCONFIRMED, que
   bloqueia novos trades até resolução manual (`main.py ledger resolve`).
"""

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from trader.ledger import Ledger
from trader.ledger.ledger import DATA_DIR
from trader.models.intent import IntentRecord, TradeIntent
from trader.models.order import SwapResult
from trader.policy import Policy, evaluate
from trader.providers.jupiter.async_jupiter_svc import TransactionSubmittedError

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


class KillSwitch:
    """Arquivo-flag: se existe, nenhum trade é executado (fail-closed)."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else DATA_DIR / "HALT"

    def is_active(self) -> bool:
        try:
            return self.path.exists()
        except OSError:
            return True  # não conseguiu ler: assume parado

    def activate(self, reason: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(reason, encoding="utf-8")

    def deactivate(self) -> None:
        self.path.unlink(missing_ok=True)


class TradeGateway:
    def __init__(
        self,
        ledger: Ledger,
        policy: Policy,
        kill_switch: KillSwitch,
        real_mode: bool,
    ):
        self.ledger = ledger
        self.policy = policy
        self.kill_switch = kill_switch
        self.real_mode = real_mode

    async def submit(
        self,
        intent: TradeIntent,
        execute: Callable[[], Awaitable[SwapResult]],
    ) -> SwapResult:
        self._authorize(intent)
        result = await self._execute(intent, execute)
        self.ledger.mark_executed(intent.intent_id, result)
        return result

    def _authorize(self, intent: TradeIntent) -> None:
        """Idempotência + política; registra a intenção (ou a recusa)."""
        existing = self.ledger.find_by_idempotency_key(intent.idempotency_key)
        if existing is not None:
            raise DuplicateIntentError(existing)

        decision = evaluate(
            intent,
            self.policy,
            self.ledger.policy_state(),
            halted=self.kill_switch.is_active(),
            real_mode=self.real_mode,
        )
        self.ledger.record_intent(intent, decision)
        if not decision.allowed:
            logger.warning(f"Intenção {intent.intent_id} recusada: {decision.reasons}")
            raise PolicyDeniedError(intent, decision.reasons)

    async def _execute(
        self,
        intent: TradeIntent,
        execute: Callable[[], Awaitable[SwapResult]],
    ) -> SwapResult:
        """Executa e registra falhas; o sucesso é registrado por `submit`."""
        try:
            return await execute()
        except TransactionSubmittedError as ex:
            self.ledger.mark_unconfirmed(intent.intent_id, str(ex), ex.signature)
            raise
        except Exception as ex:
            # erros antes do envio (quote, simulação, política do provider)
            self.ledger.mark_failed(intent.intent_id, f"{type(ex).__name__}: {ex}")
            raise
        except BaseException as ex:
            # cancelamento (Ctrl+C) no meio da execução: não dá para saber se a
            # transação chegou a ser enviada, então exige resolução manual
            self.ledger.mark_unconfirmed(
                intent.intent_id, f"interrompida: {type(ex).__name__}"
            )
            raise

    def halt(self, reason: str) -> None:
        self.kill_switch.activate(reason)
        self.ledger.add_event("halt", {"reason": reason})

    def resume(self, note: str = "") -> None:
        self.kill_switch.deactivate()
        # rearma o circuit breaker: falhas anteriores não contam mais
        self.ledger.add_event("resume", {"note": note})
