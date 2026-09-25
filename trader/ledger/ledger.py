"""Ledger persistente (SQLite) de intenções, ordens e eventos.

É a fonte da verdade para posições, PnL, orçamentos da política e auditoria:
sobrevive a reinícios. Cada mudança de estado grava um evento encadeado por
hash (sha256 do evento anterior + conteúdo), então edições manuais no banco
são detectáveis com `verify_chain()`.

Use um arquivo por modo (`ledger_path(mode)`) para que o histórico de dry-run
nunca se misture com o real.
"""

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trader.models import SOLANA_MINTS, Order, OrderSide
from trader.models.costs import LAMPORTS_PER_SOL, PnLResult, costs_from_dict
from trader.models.intent import (
    ACTIVE_STATUSES,
    MOVED_FUNDS_STATUSES,
    IntentRecord,
    IntentSide,
    IntentStatus,
    PolicyDecision,
    TradeIntent,
)
from trader.models.order import SwapResult
from trader.policy import PolicyState

DATA_DIR = Path(".data")
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


def _int(value: int | None) -> int:
    return value or 0


def _in(statuses) -> tuple[str, tuple[str, ...]]:
    """('?, ?', ('a', 'b')) para uma cláusula `IN (...)`."""
    values = tuple(map(str, statuses))
    return ", ".join("?" for _ in values), values


def ledger_path(mode: str) -> Path:  # aceita RunningMode (StrEnum)
    return DATA_DIR / f"ledger-{mode}.sqlite3"


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _json_default(value):
    return value.isoformat() if isinstance(value, datetime) else str(value)


def order_to_json(order: Order) -> str:
    return json.dumps(asdict(order), default=_json_default, sort_keys=True)


def order_from_json(data: str) -> Order:
    raw = json.loads(data)
    decimals = (
        "quantity",
        "price",
        "requested_quantity",
        "requested_price",
        "fill_price",
        "quote_amount",
        "quote_usd",
        "sol_usd",
        "sol_in_quote",
    )
    for key in decimals:
        if raw.get(key) is not None:
            raw[key] = Decimal(raw[key])
    raw["costs"] = costs_from_dict(raw.get("costs"))
    raw["side"] = OrderSide(raw["side"])
    raw["timestamp"] = datetime.fromisoformat(raw["timestamp"])
    return Order(**raw)


class Ledger:
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

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- eventos ---------------------------------------------------------------

    def _add_event(self, type_: str, intent_id: str | None, payload: dict) -> None:
        row = self.conn.execute(
            "SELECT hash FROM events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        prev_hash = row["hash"] if row else GENESIS_HASH
        ts = datetime.now(UTC).isoformat()
        body = json.dumps(payload, default=_json_default, sort_keys=True)
        digest = _event_hash(prev_hash, ts, intent_id, type_, body)
        self.conn.execute(
            "INSERT INTO events (ts, intent_id, type, payload, prev_hash, hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts, intent_id, type_, body, prev_hash, digest),
        )

    def add_event(self, type_: str, payload: dict, intent_id: str | None = None):
        with self.conn:
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

    # --- intenções -------------------------------------------------------------

    def record_intent(self, intent: TradeIntent, decision: PolicyDecision) -> None:
        status = IntentStatus.EXECUTING if decision.allowed else IntentStatus.DENIED
        now = datetime.now(UTC).isoformat()
        with self.conn:
            self.conn.execute(
                "INSERT INTO intents (intent_id, idempotency_key, created_at, "
                "updated_at, source, account, side, spend_mint, receive_mint, "
                "spend_amount, notional_usd, price, quantity, rationale, status, "
                "decision_reasons, policy_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    intent.intent_id,
                    intent.idempotency_key,
                    intent.created_at.isoformat(),
                    now,
                    intent.source,
                    intent.account,
                    str(intent.side),
                    intent.spend_mint,
                    intent.receive_mint,
                    str(intent.spend_amount),
                    _str(intent.notional_usd),
                    _str(intent.price),
                    _str(intent.quantity),
                    intent.rationale,
                    str(status),
                    json.dumps(list(decision.reasons)),
                    decision.policy_version,
                ),
            )
            self._add_event(
                "intent_" + str(status),
                intent.intent_id,
                {
                    "intent": asdict(intent),
                    "reasons": list(decision.reasons),
                    "policy_version": decision.policy_version,
                },
            )

    def _update(self, intent_id: str, event: str, payload: dict, **columns) -> None:
        columns["updated_at"] = datetime.now(UTC).isoformat()
        assignments = ", ".join(f"{k} = ?" for k in columns)
        with self.conn:
            cursor = self.conn.execute(
                f"UPDATE intents SET {assignments} WHERE intent_id = ?",
                (*columns.values(), intent_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"intenção não encontrada: {intent_id}")
            self._add_event(event, intent_id, payload)

    def mark_executed(self, intent_id: str, result: SwapResult) -> None:
        self._update(
            intent_id,
            "intent_executed",
            asdict(result),
            status=str(IntentStatus.EXECUTED),
            signature=result.signature,
            in_amount=result.in_amount,
            out_amount=result.out_amount,
        )

    def mark_failed(self, intent_id: str, error: str) -> None:
        self._mark_error(intent_id, IntentStatus.FAILED, error)

    def mark_unconfirmed(
        self, intent_id: str, error: str, signature: str | None = None
    ) -> None:
        # a assinatura permite conferir a transação e cobrar a taxa depois
        self._mark_error(intent_id, IntentStatus.UNCONFIRMED, error, signature)

    def _mark_error(
        self,
        intent_id: str,
        status: IntentStatus,
        error: str,
        signature: str | None = None,
    ) -> None:
        extra = {"signature": signature} if signature else {}
        self._update(
            intent_id,
            f"intent_{status}",
            {"error": error, **extra},
            status=str(status),
            error=error,
            **extra,
        )

    def record_failed_fee(self, intent_id: str, fee_lamports: int) -> None:
        """Taxa paga por uma transação que falhou (sem posição associada)."""
        self._update(
            intent_id,
            "failed_fee_recorded",
            {"fee_lamports": fee_lamports},
            fee_lamports=fee_lamports,
            costs_source="onchain",
        )

    def attach_order(
        self,
        intent_id: str,
        order: Order,
        realized_pnl_usd: Decimal | None = None,
        pnl: PnLResult | None = None,
    ) -> None:
        """Grava a ordem, seus custos e (vendas) o PnL, numa só atualização."""
        self._update(
            intent_id,
            "order_recorded",
            {
                "order": asdict(order),
                "realized_pnl_usd": realized_pnl_usd,
                "pnl": asdict(pnl) if pnl else None,
            },
            order_json=order_to_json(order),
            realized_pnl_usd=_str(realized_pnl_usd),
            **_order_columns(order),
            **_pnl_columns(pnl),
        )

    def resolve(self, intent_id: str, status: IntentStatus, note: str) -> None:
        """Resolução manual de uma intenção sem confirmação."""
        if status not in (IntentStatus.EXECUTED, IntentStatus.FAILED):
            raise ValueError("resolva como executed ou failed")
        record = self.get(intent_id)
        if record is None:
            raise KeyError(f"intenção não encontrada: {intent_id}")
        if record.status not in ACTIVE_STATUSES:
            raise ValueError(f"intenção {intent_id} já está {record.status}")
        self._update(
            intent_id,
            "intent_resolved",
            {"status": str(status), "note": note},
            status=str(status),
            error=note if status == IntentStatus.FAILED else record.error,
        )

    # --- consultas -------------------------------------------------------------

    def _record(self, row: sqlite3.Row) -> IntentRecord:
        intent = TradeIntent(
            intent_id=row["intent_id"],
            idempotency_key=row["idempotency_key"],
            created_at=datetime.fromisoformat(row["created_at"]),
            source=row["source"],
            account=row["account"],
            side=IntentSide(row["side"]),
            spend_mint=row["spend_mint"],
            receive_mint=row["receive_mint"],
            spend_amount=Decimal(row["spend_amount"]),
            notional_usd=_dec(row["notional_usd"]),
            price=_dec(row["price"]),
            quantity=_dec(row["quantity"]),
            rationale=row["rationale"],
        )
        return IntentRecord(
            intent=intent,
            status=IntentStatus(row["status"]),
            decision_reasons=tuple(json.loads(row["decision_reasons"])),
            policy_version=row["policy_version"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
            signature=row["signature"],
            in_amount=row["in_amount"],
            out_amount=row["out_amount"],
            error=row["error"],
            order_json=row["order_json"],
            realized_pnl_usd=_dec(row["realized_pnl_usd"]),
            fee_lamports=row["fee_lamports"],
            rent_lamports=row["rent_lamports"],
            other_lamports=row["other_lamports"],
            costs_source=row["costs_source"],
            quote_mint=row["quote_mint"],
            net_pnl_quote=_dec(row["net_pnl_quote"]),
        )

    def get(self, intent_id: str) -> IntentRecord | None:
        row = self.conn.execute(
            "SELECT * FROM intents WHERE intent_id = ?", (intent_id,)
        ).fetchone()
        return self._record(row) if row else None

    def find_by_idempotency_key(self, key: str) -> IntentRecord | None:
        """Intenção com a chave que executou ou pode ter executado.

        Intenções recusadas ou que falharam antes do envio não movimentaram
        fundos, então não bloqueiam uma nova tentativa com a mesma chave.
        """
        placeholders, statuses = _in(MOVED_FUNDS_STATUSES)
        row = self.conn.execute(
            f"SELECT * FROM intents WHERE idempotency_key = ? "
            f"AND status IN ({placeholders}) ORDER BY created_at DESC LIMIT 1",
            (key, *statuses),
        ).fetchone()
        return self._record(row) if row else None

    def list_intents(self, limit: int = 20) -> list[IntentRecord]:
        rows = self.conn.execute(
            "SELECT * FROM intents ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._record(r) for r in rows]

    def last_executed_trade(self, account: str) -> IntentRecord | None:
        row = self.conn.execute(
            "SELECT * FROM intents WHERE account = ? AND status = ? "
            "AND side IN (?, ?) AND order_json IS NOT NULL "
            "ORDER BY updated_at DESC LIMIT 1",
            (
                account,
                str(IntentStatus.EXECUTED),
                str(IntentSide.BUY),
                str(IntentSide.SELL),
            ),
        ).fetchone()
        return self._record(row) if row else None

    def pnl_totals(self, account: str) -> AccountPnL:
        return self.pnl_report(account).get(account) or AccountPnL(account)

    def pnl_report(self, account: str | None = None) -> dict[str, AccountPnL]:
        """PnL e custos por conta, a partir das intenções executadas."""
        query = "SELECT * FROM intents WHERE status = ?"
        params: tuple = (str(IntentStatus.EXECUTED),)
        if account is not None:
            query += " AND account = ?"
            params += (account,)
        report: dict[str, AccountPnL] = {}
        for row in self.conn.execute(query + " ORDER BY created_at", params):
            report.setdefault(row["account"], AccountPnL(row["account"])).add(row)
        self._add_failed_fees(report, account)
        return report

    def _add_failed_fees(self, report: dict, account: str | None) -> None:
        rows = self.conn.execute(
            "SELECT account, fee_lamports FROM intents WHERE status != ? "
            "AND fee_lamports IS NOT NULL",
            (str(IntentStatus.EXECUTED),),
        ).fetchall()
        for row in rows:
            if account is None or row["account"] == account:
                entry = report.setdefault(row["account"], AccountPnL(row["account"]))
                entry.failed_fee_lamports += row["fee_lamports"]

    def total_realized_pnl(self, account: str) -> Decimal:
        rows = self.conn.execute(
            "SELECT realized_pnl_usd FROM intents WHERE account = ? "
            "AND realized_pnl_usd IS NOT NULL",
            (account,),
        ).fetchall()
        return sum((Decimal(r["realized_pnl_usd"]) for r in rows), Decimal("0"))

    def policy_state(self, now: datetime | None = None) -> PolicyState:
        now = now or datetime.now(UTC)
        day_ago = (now - timedelta(hours=24)).isoformat()
        hour_ago = (now - timedelta(hours=1)).isoformat()
        daily_notional, trades_last_hour = self._spending(day_ago, hour_ago)
        return PolicyState(
            daily_notional_usd=daily_notional,
            trades_last_hour=trades_last_hour,
            daily_realized_pnl_usd=self._realized_pnl_since(day_ago),
            consecutive_failures=self._consecutive_failures(),
            unresolved_intent_ids=self._unresolved_ids(),
        )

    def _spending(self, day_ago: str, hour_ago: str) -> tuple[Decimal, int]:
        """Compras/swaps que moveram fundos: valor em 24h e contagem em 1h."""
        placeholders, statuses = _in(MOVED_FUNDS_STATUSES)
        rows = self.conn.execute(
            f"SELECT notional_usd, created_at FROM intents WHERE created_at >= ? "
            f"AND side != ? AND status IN ({placeholders})",
            (day_ago, str(IntentSide.SELL), *statuses),
        ).fetchall()
        notional = sum(
            (Decimal(r["notional_usd"]) for r in rows if r["notional_usd"]),
            Decimal("0"),
        )
        return notional, sum(1 for r in rows if r["created_at"] >= hour_ago)

    def _realized_pnl_since(self, since: str) -> Decimal:
        rows = self.conn.execute(
            "SELECT realized_pnl_usd FROM intents WHERE status = ? "
            "AND updated_at >= ? AND realized_pnl_usd IS NOT NULL",
            (str(IntentStatus.EXECUTED), since),
        ).fetchall()
        return sum((Decimal(r["realized_pnl_usd"]) for r in rows), Decimal("0"))

    def _consecutive_failures(self) -> int:
        """Falhas desde o último sucesso ou o último `resume`."""
        resumed_at = self.last_event_time("resume")
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM intents WHERE status = ? AND updated_at > "
            "MAX(?, COALESCE((SELECT MAX(updated_at) FROM intents "
            "WHERE status = ?), ''))",
            (
                str(IntentStatus.FAILED),
                resumed_at.isoformat() if resumed_at else "",
                str(IntentStatus.EXECUTED),
            ),
        ).fetchone()
        return row["n"]

    def _unresolved_ids(self) -> tuple[str, ...]:
        placeholders, statuses = _in(ACTIVE_STATUSES)
        rows = self.conn.execute(
            f"SELECT intent_id FROM intents WHERE status IN ({placeholders}) "
            "ORDER BY created_at",
            statuses,
        ).fetchall()
        return tuple(r["intent_id"] for r in rows)


def _event_hash(
    prev_hash: str, ts: str, intent_id: str | None, type_: str, payload: str
) -> str:
    content = "|".join((prev_hash, ts, intent_id or "", type_, payload))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _order_columns(order: Order) -> dict:
    costs = order.costs
    columns: dict = {
        "quote_mint": order.input_mint,
        "sol_usd_price": _str(order.sol_usd),
        "quote_usd_price": _str(order.quote_usd),
    }
    if costs is None:
        return columns
    cost_usd = (
        costs.native_cost_sol * order.sol_usd if order.sol_usd is not None else None
    )
    return columns | {
        "fee_lamports": costs.fee_lamports,
        "priority_fee_lamports": costs.priority_fee_lamports,
        "rent_lamports": costs.rent_lamports,
        "other_lamports": costs.other_lamports,
        "costs_source": costs.source,
        "costs_usd": _str(cost_usd),
    }


def _pnl_columns(pnl: PnLResult | None) -> dict:
    if pnl is None:
        return {}
    return {
        "gross_pnl_quote": str(pnl.gross_quote),
        "pnl_costs_sol": str(pnl.costs_sol),
        "costs_quote": _str(pnl.costs_quote),
        "net_pnl_quote": _str(pnl.net_quote),
        "gross_pnl_usd": _str(pnl.gross_usd),
        "pnl_complete": int(pnl.complete),
    }


@dataclass
class AccountPnL:
    """Totais de uma conta: PnL nativo das posições fechadas e custos pagos."""

    account: str
    quote_symbol: str = "?"
    trades: int = 0  # pernas executadas (compras e vendas)
    closed: int = 0  # posições fechadas
    incomplete: int = 0  # fechadas sem custos convertidos (ou sem dados nativos)
    gross_quote: Decimal = Decimal("0")
    net_quote: Decimal = Decimal("0")
    costs_sol: Decimal = Decimal("0")  # custos das posições fechadas
    net_usd: Decimal = Decimal("0")
    # tudo que foi pago em SOL (inclui posições abertas)
    fee_lamports: int = 0
    priority_fee_lamports: int = 0
    rent_lamports: int = 0
    other_lamports: int = 0
    failed_fee_lamports: int = 0  # taxas de transações que falharam
    unknown_costs: int = 0  # pernas cujos custos reais não foram obtidos

    def __post_init__(self) -> None:
        # linhas antigas não têm quote_mint: o par está no nome da conta
        # (ex: "paper:SOL-USDC" -> USDC)
        if self.quote_symbol == "?" and "-" in self.account:
            self.quote_symbol = self.account.rsplit("-", 1)[1]

    @property
    def paid_sol(self) -> Decimal:
        paid = (
            self.fee_lamports
            + self.rent_lamports
            + self.other_lamports
            + self.failed_fee_lamports
        )
        return Decimal(paid) / LAMPORTS_PER_SOL

    def add(self, row: sqlite3.Row) -> None:
        self.trades += 1
        if row["quote_mint"]:
            self.quote_symbol = SOLANA_MINTS.symbol_of(row["quote_mint"])
        self.fee_lamports += _int(row["fee_lamports"])
        self.priority_fee_lamports += _int(row["priority_fee_lamports"])
        self.rent_lamports += _int(row["rent_lamports"])
        self.other_lamports += _int(row["other_lamports"])
        self.unknown_costs += row["costs_source"] in (None, "quote")
        if row["realized_pnl_usd"] is not None:
            self._add_closed(row)

    def _add_closed(self, row: sqlite3.Row) -> None:
        self.closed += 1
        self.net_usd += Decimal(row["realized_pnl_usd"])
        if row["gross_pnl_quote"] is None:
            self.incomplete += 1  # ordem antiga, sem valores nativos
            return
        gross = Decimal(row["gross_pnl_quote"])
        self.gross_quote += gross
        self.costs_sol += Decimal(row["pnl_costs_sol"] or "0")
        net = row["net_pnl_quote"]
        self.net_quote += gross if net is None else Decimal(net)
        self.incomplete += 0 if row["pnl_complete"] else 1
