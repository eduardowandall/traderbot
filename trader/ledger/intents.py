"""Intenções no ledger: registro, ciclo de vida, ordens e consultas.

Recusas idênticas seguidas na mesma conta (mesmo lado, mints e motivos)
viram uma linha só, com `repeat_count`: vários runners batendo num limite
não enchem o ledger. A repetição não grava evento; a primeira recusa está na
cadeia, e recusas não movem fundos.
"""

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal

from trader.ledger.store import LedgerStore, _dec, _in, _int, _now, _str
from trader.models.costs import PnLResult
from trader.models.intent import (
    ACTIVE_STATUSES,
    MOVED_FUNDS_STATUSES,
    IntentRecord,
    IntentSide,
    IntentStatus,
    PolicyDecision,
    TradeIntent,
)
from trader.models.order import Order, SwapResult, order_to_json


class IntentStore(LedgerStore):
    def record_intent(self, intent: TradeIntent, decision: PolicyDecision) -> None:
        status = IntentStatus.EXECUTING if decision.allowed else IntentStatus.DENIED
        with self._write():
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
            "spend_amount, notional_usd, price, quantity, rationale, status, "
            "decision_reasons, policy_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                str(status),
                json.dumps(list(decision.reasons)),
                decision.policy_version,
            ),
        )

    def _update(self, intent_id: str, event: str, payload: dict, **columns) -> None:
        columns["updated_at"] = _now()
        assignments = ", ".join(f"{k} = ?" for k in columns)
        with self._write():
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
        return _record(row) if row else None


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
