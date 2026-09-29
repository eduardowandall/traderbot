"""Agregados do ledger que a política usa (`PolicyState`)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trader.ledger.store import LedgerStore, _in
from trader.models.intent import (
    ACTIVE_STATUSES,
    MOVED_FUNDS_STATUSES,
    IntentSide,
    IntentStatus,
)
from trader.policy import PolicyState


class PolicyStateQueries(LedgerStore):
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
