"""Ledger persistente (SQLite) de intenções, ordens e eventos.

É a fonte da verdade para posições, PnL, orçamentos da política e auditoria:
sobrevive a reinícios. `Ledger` junta as partes, que dividem uma conexão:

- `store.py`: conexão, esquema e o log de eventos;
- `intents.py`: registro e ciclo de vida das intenções, ordens, consultas;
- `reports.py`: PnL e custos por conta;
- `policy_state.py`: os agregados que a política usa;
- `positions.py`: a posição aberta de cada conta, das pernas dela.

Use um arquivo por modo (`ledger_path(mode)`) para que o histórico de paper
nunca se misture com o real.
"""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from trader.execution.models.intent import IntentRecord, PolicyDecision, TradeIntent
from trader.execution.trade.ledger.intents import IntentStore
from trader.execution.trade.ledger.policy_state import PolicyStateQueries
from trader.execution.trade.ledger.positions import open_entry, open_position
from trader.execution.trade.ledger.reports import AccountPnL, Reports
from trader.execution.trade.policy import PolicyState
from trader.shared.models.direction import Direction
from trader.shared.models.order import Order
from trader.shared.models.position import Position
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

    def open_entry(self, account: str) -> tuple[Order, str] | None:
        """A entrada da posição aberta da conta e a intenção que a abriu."""
        opened = self.open_position(account)
        return None if opened is None else (opened[0].entry_order, opened[1])

    def open_position(self, account: str) -> tuple[Position, str] | None:
        """A posição aberta (com as contagens dela, A20) e a intenção que a abriu."""
        legs = self.legs_since_last_buy(account)
        position = open_position(legs)
        return None if position is None else (position, legs[0].intent.intent_id)

    def _open_entries(self, prefix: str) -> list[Order]:
        """A entrada aberta de cada conta do prefixo."""
        entries = (
            open_entry(self.legs_since_last_buy(a)) for a in self.accounts(prefix)
        )
        return [entry for entry in entries if entry is not None]

    def open_perp_markets(self, prefix: str = "") -> list[tuple[str, Direction]]:
        """(mercado, lado) das perps abertas nas contas do prefixo (A12)."""
        return [
            (entry.output_mint, entry.perp.direction)
            for entry in self._open_entries(prefix)
            if entry.perp is not None
        ]

    def open_positions(self, prefix: str = "") -> dict[str, Decimal]:
        """Tokens em posições abertas, somados por mint, nas contas do prefixo.

        Só spot: uma perp fica no venue, não na carteira (A8).
        """
        held: dict[str, Decimal] = {}
        for account in self.accounts(prefix):
            entry = open_entry(self.legs_since_last_buy(account))
            if entry is not None and entry.perp is None:
                mint = entry.output_mint
                held[mint] = held.get(mint, Decimal("0")) + entry.quantity
        return held

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
