"""Compare a live bucket with a backtest of the ticks it saw, as JSON.

Record the ticks while the spec runs (`main.py run paper SPEC --record-ticks
FILE`), then replay them: the strategy warms up on the candles that closed
before the first tick (as the bot does at startup), trades on the ticks, and
the result is printed next to the legs the bucket `<mode>:strategy:<spec_id>`
executed in the same window, with the differences (trade count, realized PnL,
fill prices). Reads the ledger read-only and fetches public candles; no key.

Usage (from the project root):
    uv run --no-sync python .claude/scripts/live_vs_backtest.py SPEC.json
        --ticks FILE [--mode paper] [--fee-bps N] [--slippage-bps 10]
        [--network-fee-usd N] [--seed 0]

Like `backtest`, the pair fee and the network fee are measured on Jupiter now
unless both `--fee-bps` and `--network-fee-usd` are given (B9).

Honours TRADER_DATA_DIR, so it reads a `/smoke` run's ledger when pointed there.
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trader.api.cli.output import dumps, errors_of
from trader.backtest import load_ticks
from trader.backtest.compare import compare_live, fetch_warmup
from trader.backtest.costs import resolve_costs
from trader.backtest.spec import ReplayCosts
from trader.execution.market import JupiterMarketData
from trader.execution.market.jupiter.async_jupiter_client import AsyncJupiterClient
from trader.execution.trade.ledger import Ledger, ledger_path
from trader.shared.market.pair import market_for
from trader.strategy.spec.parse import parse_spec


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("spec", type=Path)
    parser.add_argument("--ticks", type=Path, required=True)
    parser.add_argument("--mode", default="paper", choices=["paper", "real"])
    parser.add_argument("--fee-bps", type=Decimal)
    parser.add_argument("--slippage-bps", type=Decimal, default=Decimal("10"))
    parser.add_argument("--network-fee-usd", type=Decimal)
    parser.add_argument("--seed", default="0")
    return parser.parse_args()


async def _compare(args: argparse.Namespace) -> dict:
    spec = parse_spec(args.spec.read_text(encoding="utf-8"))
    ticks = load_ticks(args.ticks)
    if not ticks:
        raise ValueError(f"{args.ticks}: nenhum tick")
    path = ledger_path(args.mode)
    if not path.exists():
        raise ValueError(f"{path}: ledger não existe")
    data = market_for(spec.symbol, JupiterMarketData)  # razão num par sem stable
    try:
        warmup = await fetch_warmup(data, spec, ticks[0].timestamp, datetime.now(UTC))
    finally:
        await data.aclose()
    costs = await _costs(args, spec)
    with Ledger(path) as ledger:
        return await compare_live(
            spec, ticks, ledger, args.mode, warmup, costs, args.seed
        )


async def _costs(args: argparse.Namespace, spec) -> ReplayCosts:
    """The given costs; a missing fee or network fee is measured now."""
    fee, network, _ = await resolve_costs(
        AsyncJupiterClient, spec, args.fee_bps, args.network_fee_usd
    )
    return ReplayCosts(fee, args.slippage_bps, network)


def main() -> int:
    # the Windows console is cp1252; Claude reads UTF-8 and nothing may crash
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    args = _parse_args()
    try:
        report = asyncio.run(_compare(args))
    except Exception as ex:
        print(dumps({"ok": False, "errors": errors_of(ex)}))
        return 1
    print(dumps({"ok": True, **report}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
