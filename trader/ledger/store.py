"""Base do ledger: conexão SQLite, esquema, migrações e a cadeia de eventos.

Cada mudança de estado grava um evento encadeado por hash (sha256 do evento
anterior + conteúdo), então edições manuais no banco são detectáveis com
`verify_chain()`. Toda escrita passa por `_write()` (`BEGIN IMMEDIATE`).
"""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

GENESIS_HASH = "0" * 64

_SCHEMA = """
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
    status TEXT NOT NULL,
    decision_reasons TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    signature TEXT,
    in_amount INTEGER,
    out_amount INTEGER,
    error TEXT,
    order_json TEXT,
    realized_pnl_usd TEXT
);
CREATE INDEX IF NOT EXISTS ix_intents_key ON intents (idempotency_key);
CREATE INDEX IF NOT EXISTS ix_intents_account ON intents (account, updated_at);
CREATE INDEX IF NOT EXISTS ix_intents_status ON intents (status, updated_at);
CREATE INDEX IF NOT EXISTS ix_intents_created ON intents (created_at);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    intent_id TEXT,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_type ON events (type, id);
"""


# colunas adicionadas depois da primeira versão: aplicadas por migração
# (ALTER TABLE) em arquivos antigos e também em bancos novos
_ADDED_COLUMNS = {
    # custos da perna (em SOL/lamports)
    "fee_lamports": "INTEGER",
    "priority_fee_lamports": "INTEGER",
    "rent_lamports": "INTEGER",
    "other_lamports": "INTEGER",
    "costs_source": "TEXT",
    "costs_usd": "TEXT",
    # taxas do trade usadas nas estimativas
    "quote_mint": "TEXT",
    "sol_usd_price": "TEXT",
    "quote_usd_price": "TEXT",
    # PnL da posição fechada (vendas): nativo no token de cotação + USD
    "gross_pnl_quote": "TEXT",
    "pnl_costs_sol": "TEXT",
    "costs_quote": "TEXT",
    "net_pnl_quote": "TEXT",
    "gross_pnl_usd": "TEXT",
    "pnl_complete": "INTEGER",
    # recusas idênticas seguidas somadas numa só linha (além da primeira)
    "repeat_count": "INTEGER",
}


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(intents)")}
    with conn:
        for name, kind in _ADDED_COLUMNS.items():
            if name in existing:
                continue
            try:
                conn.execute(f"ALTER TABLE intents ADD COLUMN {name} {kind}")
            except sqlite3.OperationalError as ex:
                # outro processo (bot/CLI) migrou ao mesmo tempo
                if "duplicate column" not in str(ex):
                    raise


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


def _event_hash(
    prev_hash: str, ts: str, intent_id: str | None, type_: str, payload: str
) -> str:
    content = "|".join((prev_hash, ts, intent_id or "", type_, payload))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class LedgerStore:
    """Conexão compartilhada pelas partes do ledger, e a cadeia de eventos."""

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
        self.conn.executescript(_SCHEMA)
        _migrate(self.conn)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def _write(self) -> Iterator[None]:
        """Transação de escrita que já começa com o lock (`BEGIN IMMEDIATE`).

        Sem isso, `_add_event` leria o último hash fora da transação: outro
        processo (a CLI ou outro runner) poderia gravar um evento entre a
        leitura e o INSERT, e os dois eventos apontariam para o mesmo
        `prev_hash`, bifurcando a cadeia. Não guarde o hash em memória pelo
        mesmo motivo.
        """
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            yield

    # --- eventos ---------------------------------------------------------------

    def _add_event(self, type_: str, intent_id: str | None, payload: dict) -> None:
        row = self.conn.execute(
            "SELECT hash FROM events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        prev_hash = row["hash"] if row else GENESIS_HASH
        ts = _now()
        body = json.dumps(payload, default=_json_default, sort_keys=True)
        digest = _event_hash(prev_hash, ts, intent_id, type_, body)
        self.conn.execute(
            "INSERT INTO events (ts, intent_id, type, payload, prev_hash, hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts, intent_id, type_, body, prev_hash, digest),
        )

    def add_event(self, type_: str, payload: dict, intent_id: str | None = None):
        with self._write():
            self._add_event(type_, intent_id, payload)

    def verify_chain(self) -> int | None:
        """Retorna o id do primeiro evento adulterado, ou None se íntegro."""
        prev_hash = GENESIS_HASH
        for row in self.conn.execute("SELECT * FROM events ORDER BY id"):
            expected = _event_hash(
                prev_hash, row["ts"], row["intent_id"], row["type"], row["payload"]
            )
            if row["prev_hash"] != prev_hash or row["hash"] != expected:
                return row["id"]
            prev_hash = row["hash"]
        return None

    def last_event_time(self, type_: str) -> datetime | None:
        row = self.conn.execute(
            "SELECT ts FROM events WHERE type = ? ORDER BY id DESC LIMIT 1", (type_,)
        ).fetchone()
        return datetime.fromisoformat(row["ts"]) if row else None
