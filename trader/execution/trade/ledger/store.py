"""Base do ledger: conexão SQLite, esquema e o log de eventos.

Cada mudança de estado grava um evento (tabela `events`), a trilha de
auditoria. Toda escrita passa por `_write()` (`BEGIN IMMEDIATE`).

O esquema tem uma versão (`PRAGMA user_version`). Um arquivo de outra versão
não é migrado: abrir falha com `LedgerFormatError`, e o dono move ou apaga o
arquivo.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

SCHEMA_VERSION = 3

# numa transação só: outro processo criando ao mesmo tempo espera e não
# vê um banco pela metade
_SCHEMA = f"""
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS intents (
    intent_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source TEXT NOT NULL,
    account TEXT NOT NULL,
    side TEXT NOT NULL,
    spend_mint TEXT NOT NULL,
    receive_mint TEXT NOT NULL,
    spend_amount TEXT NOT NULL,
    notional_usd TEXT,
    price TEXT,
    quantity TEXT,
    rationale TEXT,
    closes_position INTEGER,
    status TEXT NOT NULL,
    decision_reasons TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    signature TEXT,
    in_amount INTEGER,
    out_amount INTEGER,
    error TEXT,
    order_json TEXT,
    realized_pnl_usd TEXT,
    -- custos da perna (em SOL/lamports)
    fee_lamports INTEGER,
    priority_fee_lamports INTEGER,
    rent_lamports INTEGER,
    other_lamports INTEGER,
    costs_source TEXT,
    costs_usd TEXT,
    -- taxas do trade usadas nas estimativas
    quote_mint TEXT,
    sol_usd_price TEXT,
    quote_usd_price TEXT,
    -- PnL da posição fechada (vendas): nativo no token de cotação + USD
    gross_pnl_quote TEXT,
    pnl_costs_sol TEXT,
    costs_quote TEXT,
    net_pnl_quote TEXT,
    gross_pnl_usd TEXT,
    pnl_complete INTEGER,
    -- recusas idênticas seguidas somadas numa só linha (além da primeira)
    repeat_count INTEGER,
    -- spot ou perp (A8); numa perp, o mercado, o lado e a alavancagem
    instrument TEXT NOT NULL DEFAULT 'spot',
    perp_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_intents_key ON intents (idempotency_key);
CREATE INDEX IF NOT EXISTS ix_intents_account ON intents (account, updated_at);
CREATE INDEX IF NOT EXISTS ix_intents_status ON intents (status, updated_at);
CREATE INDEX IF NOT EXISTS ix_intents_created ON intents (created_at);
-- uma chave de idempotência só pode estar em uma intenção que moveu fundos
CREATE UNIQUE INDEX IF NOT EXISTS ux_intents_moved_key ON intents (idempotency_key)
    WHERE status IN ('executing', 'executed', 'unconfirmed');
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    intent_id TEXT,
    type TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_type ON events (type, id);
PRAGMA user_version = {SCHEMA_VERSION};
COMMIT;
"""


class LedgerFormatError(Exception):
    """O arquivo é de outra versão do esquema: mova ou apague."""


# --- ajudantes de SQL ---------------------------------------------------------


def _int(value: int | None) -> int:
    return value or 0


def _in(statuses) -> tuple[str, tuple[str, ...]]:
    """('?, ?', ('a', 'b')) para uma cláusula `IN (...)`."""
    values = tuple(map(str, statuses))
    return ", ".join("?" for _ in values), values


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _json_default(value):
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _window(
    column: str, start: datetime | None, end: datetime | None
) -> tuple[str, tuple[str, ...]]:
    """Cláusula `[start, end)` sobre uma coluna ISO em UTC (vazia sem limites)."""
    sql, params = "", ()
    if start is not None:
        sql, params = f" AND {column} >= ?", (start.astimezone(UTC).isoformat(),)
    if end is not None:
        sql = f"{sql} AND {column} < ?"
        params = (*params, end.astimezone(UTC).isoformat())
    return sql, params


def _open_schema(conn: sqlite3.Connection, path: str) -> None:
    """Cria o esquema num banco vazio; recusa um de outra versão."""
    has_tables = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'intents'"
    ).fetchone()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if has_tables and version != SCHEMA_VERSION:
        raise LedgerFormatError(
            f"ledger {path} está num formato antigo (versão {version}, esperada "
            f"{SCHEMA_VERSION}): mova ou apague o arquivo"
        )
    if not has_tables:
        conn.executescript(_SCHEMA)


class LedgerStore:
    """Conexão compartilhada pelas partes do ledger, e o log de eventos."""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        in_memory = self.path == ":memory:"
        if not in_memory:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        if not in_memory:
            self.conn.execute("PRAGMA journal_mode=WAL")
            # seguro com WAL: evita um fsync por commit no loop do bot
            self.conn.execute("PRAGMA synchronous=NORMAL")
        try:
            _open_schema(self.conn, self.path)
        except Exception:
            self.conn.close()
            raise

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def _write(self) -> Iterator[None]:
        """Transação de escrita que já começa com o lock (`BEGIN IMMEDIATE`).

        Leitura e escrita ficam na mesma transação: outro processo (outro bot)
        não grava entre as duas.
        """
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            yield

    # --- eventos ---------------------------------------------------------------

    def _add_event(self, type_: str, intent_id: str | None, payload: dict) -> None:
        body = json.dumps(payload, default=_json_default, sort_keys=True)
        self.conn.execute(
            "INSERT INTO events (ts, intent_id, type, payload) VALUES (?, ?, ?, ?)",
            (_now(), intent_id, type_, body),
        )

    def add_event(self, type_: str, payload: dict, intent_id: str | None = None):
        with self._write():
            self._add_event(type_, intent_id, payload)
