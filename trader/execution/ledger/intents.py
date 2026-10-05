"""Intenções no ledger: registro, ciclo de vida, ordens e consultas.

Recusas idênticas seguidas na mesma conta (mesmo lado, mints e motivos)
viram uma linha só, com `repeat_count`: vários runners batendo num limite
não enchem o ledger. A repetição não grava evento; a primeira recusa está no
log, e recusas não movem fundos.
"""

import json
import logging
import sqlite3
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal

from trader.execution.ledger.store import (
    LedgerStore,
    _dec,
    _in,
    _int,
    _now,
    _str,
    _window,
)
from trader.execution.models.intent import (
    ACTIVE_STATUSES,
    MOVED_FUNDS_STATUSES,
    IntentRecord,
    IntentSide,
    IntentStatus,
    PolicyDecision,
    TradeIntent,
)
from trader.shared.models.costs import PnLResult
from trader.shared.models.order import Order, SwapResult, order_from_json, order_to_json

logger = logging.getLogger(__name__)


class IntentStore(LedgerStore):
    def record_intent(self, intent: TradeIntent, decision: PolicyDecision) -> None:
        with self._write():
            self._record_locked(intent, decision)

    def _record_locked(self, intent: TradeIntent, decision: PolicyDecision) -> None:
        """Grava a intenção (ou soma a recusa repetida); exige `_write()`."""
        status = IntentStatus.EXECUTING if decision.allowed else IntentStatus.DENIED
        if not decision.allowed and self._count_repeat(intent, decision):
            return
        self._insert(intent, decision, status)
        self._add_event(
            "intent_" + str(status),
            intent.intent_id,
            {
                "intent": asdict(intent),
                "reasons": list(decision.reasons),
                "policy_version": decision.policy_version,
            },
        )

    def _count_repeat(self, intent: TradeIntent, decision: PolicyDecision) -> bool:
        """Soma a recusa na última linha da conta, se for a mesma recusa."""
        last = self.conn.execute(
            "SELECT rowid, status, side, spend_mint, receive_mint, decision_reasons "
            "FROM intents WHERE account = ? ORDER BY rowid DESC LIMIT 1",
            (intent.account,),
        ).fetchone()
        if last is None or not _same_denial(last, intent, decision):
            return False
        self.conn.execute(
            "UPDATE intents SET repeat_count = COALESCE(repeat_count, 0) + 1, "
            "updated_at = ? WHERE rowid = ?",
            (_now(), last["rowid"]),
        )
        return True

    def _insert(
        self, intent: TradeIntent, decision: PolicyDecision, status: IntentStatus
    ) -> None:
        self.conn.execute(
            "INSERT INTO intents (intent_id, idempotency_key, created_at, "
            "updated_at, source, account, side, spend_mint, receive_mint, "
            "spend_amount, notional_usd, price, quantity, rationale, "
            "closes_position, status, decision_reasons, policy_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                intent.intent_id,
                intent.idempotency_key,
                intent.created_at.isoformat(),
                _now(),
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
                None if intent.closes_position is None else int(intent.closes_position),
                str(status),
                json.dumps(list(decision.reasons)),
                decision.policy_version,
            ),
        )

    def _update(self, intent_id: str, event: str, payload: dict, **columns) -> None:
        with self._write():
            self._update_locked(intent_id, event, payload, **columns)

    def _update_locked(
        self, intent_id: str, event: str, payload: dict, **columns
    ) -> None:
        columns["updated_at"] = _now()
        assignments = ", ".join(f"{k} = ?" for k in columns)
        cursor = self.conn.execute(
            f"UPDATE intents SET {assignments} WHERE intent_id = ?",
            (*columns.values(), intent_id),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"intenção não encontrada: {intent_id}")
        self._add_event(event, intent_id, payload)

    def _transition(self, intent_id: str, event: str, payload: dict, **columns) -> bool:
        """Muda o status só se a intenção ainda está ativa (executando/pendente).

        Uma mudança tardia (a intenção já saiu do estado ativo) é ignorada e
        registrada em ERROR, com o que teria sido gravado. Devolve se mudou.
        """
        with self._write():
            status = self._status_of(intent_id)
            if status not in ACTIVE_STATUSES:
                logger.error(
                    f"Intenção {intent_id} já está {status}: {event} ignorado "
                    f"({payload})"
                )
                return False
            self._update_locked(intent_id, event, payload, **columns)
            return True

    def _status_of(self, intent_id: str) -> IntentStatus:
        row = self.conn.execute(
            "SELECT status FROM intents WHERE intent_id = ?", (intent_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"intenção não encontrada: {intent_id}")
        return IntentStatus(row["status"])

    def mark_executed(self, intent_id: str, result: SwapResult) -> bool:
        return self._transition(
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

    def mark_rejected(self, intent_id: str, error: str) -> None:
        self._mark_error(intent_id, IntentStatus.REJECTED, error)

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
        self._transition(
            intent_id,
            f"intent_{status}",
            {"error": error, **extra},
            status=str(status),
            error=error,
            **extra,
        )

    def attach_order(
        self,
        intent_id: str,
        order: Order,
        realized_pnl_usd: Decimal | None = None,
        pnl: PnLResult | None = None,
    ) -> None:
        """Grava a ordem, seus custos e (vendas) o PnL, numa só atualização.

        Só numa intenção EXECUTED: a ordem e o PnL de uma que falhou contariam
        como trade no PnL e no custo.
        """
        status = self._status_of(intent_id)
        if status != IntentStatus.EXECUTED:
            raise ValueError(f"intenção {intent_id} está {status}, não executed")
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

    # --- consultas -------------------------------------------------------------

    def get(self, intent_id: str) -> IntentRecord | None:
        row = self.conn.execute(
            "SELECT * FROM intents WHERE intent_id = ?", (intent_id,)
        ).fetchone()
        return _record(row) if row else None

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
        return _record(row) if row else None

    def list_intents(self, limit: int = 20) -> list[IntentRecord]:
        rows = self.conn.execute(
            "SELECT * FROM intents ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_record(r) for r in rows]

    def accounts(self, prefix: str = "") -> list[str]:
        """Contas com alguma intenção (as de um modo: prefixo `"<modo>:"`)."""
        rows = self.conn.execute(
            "SELECT DISTINCT account FROM intents WHERE substr(account, 1, ?) = ? "
            "ORDER BY account",
            (len(prefix), prefix),
        ).fetchall()
        return [r["account"] for r in rows]

    def legs_since_last_buy(self, account: str) -> list[IntentRecord]:
        """A última compra executada da conta e as vendas depois dela.

        Pela ordem de criação: `updated_at` muda depois (a ordem gravada), e
        não pode reordenar as pernas.
        """
        buy = self.conn.execute(
            "SELECT created_at FROM intents WHERE account = ? AND status = ? "
            "AND side = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (account, str(IntentStatus.EXECUTED), str(IntentSide.BUY)),
        ).fetchone()
        if buy is None:
            return []
        rows = self.conn.execute(
            "SELECT * FROM intents WHERE account = ? AND status = ? "
            "AND side IN (?, ?) AND created_at >= ? ORDER BY created_at, rowid",
            (
                account,
                str(IntentStatus.EXECUTED),
                str(IntentSide.BUY),
                str(IntentSide.SELL),
                buy["created_at"],
            ),
        ).fetchall()
        return [_record(r) for r in rows]

    def executed_between(
        self, account: str, start: datetime, end: datetime
    ) -> list[IntentRecord]:
        """Pernas executadas da conta criadas em `[start, end)`, em ordem."""
        window, params = _window("created_at", start, end)
        rows = self.conn.execute(
            f"SELECT * FROM intents WHERE account = ? AND status = ?{window} "
            "ORDER BY created_at, rowid",
            (account, str(IntentStatus.EXECUTED), *params),
        ).fetchall()
        return [_record(r) for r in rows]

    def account_times(self, account: str) -> tuple[datetime | None, datetime | None]:
        """(abertura da conta, última venda executada): UTC.

        A abertura é o evento `bucket_opened` (ou, em ledgers de antes dele, a
        primeira intenção): uma spec que nunca operou não recomeça a contar o
        `ttl_days` a cada reinício.
        """
        row = self.conn.execute(
            "SELECT MIN(created_at) AS first_intent, "
            "(SELECT MIN(ts) FROM events WHERE type = 'bucket_opened' "
            "AND json_extract(payload, '$.account') = ?) AS opened, "
            "(SELECT MAX(updated_at) FROM intents WHERE account = ? AND status = ? "
            "AND side = ?) AS exited FROM intents WHERE account = ?",
            (
                account,
                account,
                str(IntentStatus.EXECUTED),
                str(IntentSide.SELL),
                account,
            ),
        ).fetchone()
        opened = min(filter(None, (row["opened"], row["first_intent"])), default=None)
        return _dt(opened), _dt(row["exited"])

    def mark_account_opened(self, account: str) -> None:
        """Grava a abertura da conta (`bucket_opened`), só na primeira vez."""
        with self._write():
            seen = self.conn.execute(
                "SELECT 1 FROM events WHERE type = 'bucket_opened' "
                "AND json_extract(payload, '$.account') = ? LIMIT 1",
                (account,),
            ).fetchone()
            if not seen:
                self._add_event("bucket_opened", None, {"account": account})

    def last_exit_price(self, account: str) -> Decimal | None:
        """Preço da última venda, no token de cotação; None sem ordem."""
        row = self.conn.execute(
            "SELECT order_json FROM intents WHERE account = ? AND status = ? "
            "AND side = ? ORDER BY updated_at DESC LIMIT 1",
            (account, str(IntentStatus.EXECUTED), str(IntentSide.SELL)),
        ).fetchone()
        if row is None or not row["order_json"]:
            return None
        return order_from_json(row["order_json"]).quote_price


def _dt(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _same_denial(
    row: sqlite3.Row, intent: TradeIntent, decision: PolicyDecision
) -> bool:
    return (
        row["status"] == str(IntentStatus.DENIED)
        and row["side"] == str(intent.side)
        and row["spend_mint"] == intent.spend_mint
        and row["receive_mint"] == intent.receive_mint
        and json.loads(row["decision_reasons"]) == list(decision.reasons)
    )


def _record(row: sqlite3.Row) -> IntentRecord:
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
        closes_position=(
            None if row["closes_position"] is None else bool(row["closes_position"])
        ),
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
        repeat_count=_int(row["repeat_count"]),
    )


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
