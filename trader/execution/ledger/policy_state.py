"""Agregados do ledger que a política usa (`PolicyState`)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trader.execution.ledger.store import LedgerStore, _in
from trader.execution.models.intent import (
    MOVED_FUNDS_STATUSES,
    IntentSide,
    IntentStatus,
)
from trader.execution.policy import PolicyState

# uma intenção executando há mais que isso provavelmente morreu no meio
STALE_EXECUTING_SECONDS = 300


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
        daily_notional, trades_last_hour, account_trades = self._spending(
            day_ago, hour_ago, account
        )
        return PolicyState(
            daily_notional_usd=daily_notional,
            trades_last_hour=trades_last_hour,
            account_trades_last_hour=account_trades,
            daily_realized_pnl_usd=self._realized_pnl_since(day_ago),
            consecutive_failures=self._consecutive_failures(failures_since),
            unresolved_intent_ids=self._unresolved_ids(now, account),
        )

    def _spending(
        self, day_ago: str, hour_ago: str, account: str | None
    ) -> tuple[Decimal, int, int]:
        """Compras/swaps que moveram fundos: valor em 24h, contagem em 1h, e
        contagem em 1h só do bucket (0 sem conta)."""
        placeholders, statuses = _in(MOVED_FUNDS_STATUSES)
        rows = self.conn.execute(
            f"SELECT notional_usd, created_at, account FROM intents "
            f"WHERE created_at >= ? AND side != ? AND status IN ({placeholders})",
            (day_ago, str(IntentSide.SELL), *statuses),
        ).fetchall()
        notional = sum(
            (Decimal(r["notional_usd"]) for r in rows if r["notional_usd"]),
            Decimal("0"),
        )
        last_hour = [r for r in rows if r["created_at"] >= hour_ago]
        mine = sum(1 for r in last_hour if account and r["account"] == account)
        return notional, len(last_hour), mine

    def _realized_pnl_since(self, since: str) -> Decimal:
        rows = self.conn.execute(
            "SELECT realized_pnl_usd FROM intents WHERE status = ? "
            "AND updated_at >= ? AND realized_pnl_usd IS NOT NULL",
            (str(IntentStatus.EXECUTED), since),
        ).fetchall()
        return sum((Decimal(r["realized_pnl_usd"]) for r in rows), Decimal("0"))

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
