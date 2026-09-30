"""Ledger persistente (SQLite) de intenções, ordens e eventos.

É a fonte da verdade para posições, PnL, orçamentos da política e auditoria:
sobrevive a reinícios. `Ledger` junta as partes, que dividem uma conexão:

- `store.py`: conexão, esquema, migrações e a cadeia de eventos (hash);
- `intents.py`: registro e ciclo de vida das intenções, ordens, consultas;
- `reports.py`: PnL e custos por conta;
- `policy_state.py`: os agregados que a política usa.

Use um arquivo por modo (`ledger_path(mode)`) para que o histórico de dry-run
nunca se misture com o real.
"""

from collections.abc import Callable
from pathlib import Path

from trader.ledger.intents import IntentStore
from trader.ledger.policy_state import PolicyStateQueries
from trader.ledger.reports import AccountPnL, Reports
from trader.models.intent import IntentRecord, PolicyDecision, TradeIntent
from trader.paths import data_dir
from trader.policy import PolicyState

__all__ = ["AccountPnL", "Ledger", "ledger_path"]


def ledger_path(mode: str) -> Path:  # aceita RunningMode (StrEnum)
    return data_dir() / f"ledger-{mode}.sqlite3"


class Ledger(IntentStore, Reports, PolicyStateQueries):
    def authorize(
        self,
        intent: TradeIntent,
        decide: Callable[[PolicyState], PolicyDecision],
    ) -> tuple[IntentRecord | None, PolicyDecision | None]:
        """Idempotência + política + registro numa transação só.

        Devolve `(existente, None)` se a chave já moveu fundos (nada é
        gravado), ou `(None, decisão)` com a intenção já registrada. Com o
        lock desde a leitura, dois processos nunca passam juntos pelo mesmo
        limite nem usam a mesma chave duas vezes.
        """
        with self._write():
            existing = self.find_by_idempotency_key(intent.idempotency_key)
            if existing is not None:
                return existing, None
            decision = decide(self.policy_state())
            self._record_locked(intent, decision)
            return None, decision

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
