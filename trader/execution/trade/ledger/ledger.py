"""Ledger persistente (SQLite) de intenções, ordens e eventos.

É a fonte da verdade para posições, PnL, orçamentos da política e auditoria:
sobrevive a reinícios. `Ledger` junta as partes, que dividem uma conexão:

- `store.py`: conexão, esquema e o log de eventos;
- `intents.py`: registro e ciclo de vida das intenções, ordens, consultas;
- `reports.py`: PnL e custos por conta;
- `policy_state.py`: os agregados que a política usa.

Use um arquivo por modo (`ledger_path(mode)`) para que o histórico de paper
nunca se misture com o real.
"""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from trader.execution.models.intent import IntentRecord, PolicyDecision, TradeIntent
from trader.execution.trade.ledger.intents import IntentStore
from trader.execution.trade.ledger.policy_state import PolicyStateQueries
from trader.execution.trade.ledger.reports import AccountPnL, Reports
from trader.execution.trade.policy import PolicyState
from trader.shared.paths import data_dir

__all__ = ["AccountPnL", "Ledger", "ledger_path"]


def ledger_path(mode: str) -> Path:  # aceita RunningMode (StrEnum)
    return data_dir() / f"ledger-{mode}.sqlite3"


class Ledger(IntentStore, Reports, PolicyStateQueries):
    def authorize(
        self,
        intent: TradeIntent,
        decide: Callable[[PolicyState], PolicyDecision],
        failures_since: datetime | None = None,
    ) -> tuple[IntentRecord | None, PolicyDecision | None]:
        """Idempotência + política + registro numa transação só.

        Devolve `(existente, None)` se a chave já moveu fundos (nada é
        gravado), ou `(None, decisão)` com a intenção já registrada. Com o
        lock desde a leitura, dois processos nunca passam juntos pelo mesmo
        limite nem usam a mesma chave duas vezes. `failures_since` limita as
        falhas que o circuit breaker conta (o início do processo).
        """
        with self._write():
            existing = self.find_by_idempotency_key(intent.idempotency_key)
            if existing is not None:
                return existing, None
            state = self.policy_state(
                failures_since=failures_since, account=intent.account
            )
            decision = decide(state)
            self._record_locked(intent, decision)
            return None, decision

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
