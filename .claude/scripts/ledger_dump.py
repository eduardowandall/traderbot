"""Read-only dump of a mode's ledger (and the paper wallet), as JSON.

The CLI has no ledger commands (stage U), so this is how `/smoke` and
`/diagnose` look at what a run did: recent intents (status, denial reasons,
errors), the unresolved ones that block trading, each bucket's open position,
PnL per bucket and the paper wallet. It never writes; an old-format ledger is reported, not migrated.

Usage (from the project root):
    uv run --no-sync python .claude/scripts/ledger_dump.py [paper|real] [--limit 20]

Honours TRADER_DATA_DIR, so it reads a `/smoke` run's state when pointed there.
"""

import argparse
import sys
from dataclasses import asdict

from trader.api.cli.output import dumps
from trader.execution.gateway import TradeGateway
from trader.execution.ledger import Ledger, ledger_path
from trader.execution.ledger.store import LedgerFormatError
from trader.execution.policy import Policy
from trader.execution.venues.paper import SimulatedWallet
from trader.execution.wiring import paper_wallet_path
from trader.shared.models import SOLANA_MINTS


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", nargs="?", default="paper", choices=["paper", "real"])
    parser.add_argument("--limit", type=int, default=20)
    return parser.parse_args()


def _intent(record) -> dict:
    return {
        "id": record.intent.intent_id,
        "account": record.intent.account,
        "side": record.intent.side,
        "status": record.status,
        "notional_usd": record.intent.notional_usd,
        "reasons": record.decision_reasons,
        "repeat_count": record.repeat_count,
        "error": record.error,
        "realized_pnl_usd": record.realized_pnl_usd,
        "created_at": record.intent.created_at,
    }


def _buckets(ledger: Ledger, accounts: list[str]) -> tuple[dict, dict]:
    """(posição aberta, PnL) de cada bucket, de um `restore` só por bucket."""
    gateway = TradeGateway(ledger, Policy(), real_mode=False)  # só leitura
    positions, pnl = {}, {}
    for account in accounts:
        state = gateway.restore(account)
        pnl[account] = asdict(state.totals)
        entry = state.open_entry
        if entry is not None:
            positions[account] = {
                "token": SOLANA_MINTS.symbol_of(entry.output_mint),
                "quantity": entry.quantity,
                "cost": entry.quote_amount,
                "price": entry.price,
            }
    return positions, pnl


def _ledger(mode: str, limit: int) -> dict:
    path = ledger_path(mode)
    if not path.exists():
        return {"path": str(path), "exists": False}
    try:
        ledger = Ledger(path)
    except LedgerFormatError as ex:
        return {"path": str(path), "error": str(ex)}
    with ledger:
        records = ledger.list_intents(limit)
        accounts = ledger.accounts(f"{mode}:")
        positions, pnl = _buckets(ledger, accounts)
        return {
            "path": str(path),
            "intents": [_intent(r) for r in records],
            "unresolved": list(ledger.policy_state().unresolved_intent_ids),
            "positions": positions,
            "pnl": pnl,
            # tudo que cada ida e volta custou (B9), em USD e bps
            "round_trip_costs": {
                a: ledger.round_trip_costs(a).as_dict() for a in accounts
            },
            "events": ledger.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        }


def _wallet() -> dict | None:
    path = paper_wallet_path()
    if not path.exists():
        return None
    balances = SimulatedWallet(path).balances()
    return {
        "path": str(path),
        "balances": {SOLANA_MINTS.symbol_of(m): v for m, v in balances.items()},
    }


def main() -> int:
    # the Windows console is cp1252; Claude reads UTF-8 and nothing may crash
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    args = _parse_args()
    report = {"ledger": _ledger(args.mode, args.limit)}
    if args.mode == "paper":
        report["paper_wallet"] = _wallet()
    print(dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
