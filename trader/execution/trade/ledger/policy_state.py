"""Agregados do ledger que a política usa (`PolicyState`)."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trader.execution.models.intent import (
    MOVED_FUNDS_STATUSES,
    SPENDING_SIDES,
    IntentStatus,
)
from trader.execution.trade.ledger.store import LedgerStore, _in
from trader.execution.trade.policy import PolicyState

# uma intenção executando há mais que isso provavelmente morreu no meio
STALE_EXECUTING_SECONDS = 300


@dataclass(frozen=True)
class _Spending:
    """As compras que contam nos limites: o valor em 24h e quando foram."""

    notional: Decimal
    day: list[str]  # created_at (ISO) das compras das últimas 24h
    last_hour: list[str]
    mine: list[str]  # da última hora, só do bucket


def _oldest(timestamps: list[str]) -> datetime | None:
    """O mais antigo de timestamps ISO do ledger (None se vazio)."""
    return datetime.fromisoformat(min(timestamps)) if timestamps else None


class PolicyStateQueries(LedgerStore):
    def policy_state(
        self,
        now: datetime | None = None,
        failures_since: datetime | None = None,
        account: str | None = None,
    ) -> PolicyState:
        now = now or datetime.now(UTC)
        day_ago = (now - timedelta(hours=24)).isoformat()
        hour_ago = (now - timedelta(hours=1)).isoformat()
        spending = self._spending(day_ago, hour_ago, account)
        pnl, loss_since = self._realized_pnl_since(day_ago)
        return PolicyState(
            daily_notional_usd=spending.notional,
            trades_last_hour=len(spending.last_hour),
            account_trades_last_hour=len(spending.mine),
            daily_realized_pnl_usd=pnl,
            consecutive_failures=self._consecutive_failures(failures_since),
            unresolved_intent_ids=self._unresolved_ids(now, account),
            as_of=now,
            daily_notional_since=_oldest(spending.day),
            trades_last_hour_since=_oldest(spending.last_hour),
            account_trades_last_hour_since=_oldest(spending.mine),
            daily_loss_since=loss_since,
        )

    def _spending(self, day_ago: str, hour_ago: str, account: str | None) -> _Spending:
        """Compras/swaps que moveram fundos: valor em 24h, as da última hora,
        e as da última hora só do bucket (nenhuma sem conta)."""
        placeholders, statuses = _in(MOVED_FUNDS_STATUSES)
        rows = self.conn.execute(
            f"SELECT notional_usd, created_at, account FROM intents "
            f"WHERE created_at >= ? AND side IN (?, ?) AND status IN ({placeholders})",
            (day_ago, *map(str, SPENDING_SIDES), *statuses),
        ).fetchall()
        notional = sum(
            (Decimal(r["notional_usd"]) for r in rows if r["notional_usd"]),
            Decimal("0"),
        )
        day = [r["created_at"] for r in rows]
        last_hour = [r for r in rows if r["created_at"] >= hour_ago]
        mine = [
            r["created_at"] for r in last_hour if account and r["account"] == account
        ]
        return _Spending(notional, day, [r["created_at"] for r in last_hour], mine)

    def _realized_pnl_since(self, since: str) -> tuple[Decimal, datetime | None]:
        """PnL realizado desde `since` e quando foi a perda mais antiga (A29)."""
        rows = self.conn.execute(
            "SELECT realized_pnl_usd, updated_at FROM intents WHERE status = ? "
            "AND updated_at >= ? AND realized_pnl_usd IS NOT NULL",
            (str(IntentStatus.EXECUTED), since),
        ).fetchall()
        pnl = [(Decimal(r["realized_pnl_usd"]), r["updated_at"]) for r in rows]
        losses = [at for value, at in pnl if value < 0]
        return sum((value for value, _ in pnl), Decimal("0")), _oldest(losses)

    def _consecutive_failures(self, since: datetime | None) -> int:
        """Falhas desde o último sucesso (e desde `since`, se dado).

        Recusas do provider (REJECTED: impacto de preço etc.) ficam de fora:
        repeti-las ao tentar sair de um mercado ralo não pode travar o stop de
        outros buckets.
        """
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM intents WHERE status = ? AND updated_at > "
            "MAX(?, COALESCE((SELECT MAX(updated_at) FROM intents "
            "WHERE status = ?), ''))",
            (
                str(IntentStatus.FAILED),
                since.isoformat() if since else "",
                str(IntentStatus.EXECUTED),
            ),
        ).fetchone()
        return row["n"]

    def _unresolved_ids(self, now: datetime, account: str | None) -> tuple[str, ...]:
        """Sem confirmação, ou executando: da própria conta, ou há tempo demais.

        Uma conta vive num processo só, então uma intenção dela ainda
        EXECUTING é um swap cujo registro falhou (ou um processo morto): ela
        bloqueia sempre, senão a conta compraria de novo. De outra conta
        (outro bot) só bloqueia depois de `STALE_EXECUTING_SECONDS`.
        """
        stale = (now - timedelta(seconds=STALE_EXECUTING_SECONDS)).isoformat()
        rows = self.conn.execute(
            "SELECT intent_id FROM intents WHERE status = ? "
            "OR (status = ? AND (updated_at < ? OR account = ?)) ORDER BY created_at",
            (
                str(IntentStatus.UNCONFIRMED),
                str(IntentStatus.EXECUTING),
                stale,
                account,
            ),
        ).fetchall()
        return tuple(r["intent_id"] for r in rows)
