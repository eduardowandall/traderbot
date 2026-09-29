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

from pathlib import Path

from trader.ledger.intents import IntentStore
from trader.ledger.policy_state import PolicyStateQueries
from trader.ledger.reports import AccountPnL, Reports
from trader.paths import data_dir

__all__ = ["AccountPnL", "Ledger", "ledger_path"]


def ledger_path(mode: str) -> Path:  # aceita RunningMode (StrEnum)
    return data_dir() / f"ledger-{mode}.sqlite3"


class Ledger(IntentStore, Reports, PolicyStateQueries):
    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
