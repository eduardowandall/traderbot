"""Backtest a strategy spec, printing only the key metrics.

Wraps `main.py backtest --json` and trims the result down to its
headline numbers plus the first few trades. The backtest also reports format
errors in the spec; the mode's policy limits are checked by the trade-runner
(`serve`) when a `connect` says `hello`.

Usage (from the project root):
    uv run --no-sync python .claude/scripts/spec_check.py SPEC.json
        [--candles 1000 | --ticks FILE | --scenario ID|tag:NAME|all ...]
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
METRICS = (
    "bars",
    "warmup_bars",
    "ticks",
    "final_equity",
    "return_pct",
    "max_drawdown_pct",
    "closed_trades",
    "win_rate_pct",
    # B9: what costs were used (measured on Jupiter by default) and what each
    # round trip cost in the replay
    "fee_bps",
    "network_fee_usd",
    "round_trip_costs",
)
SHOWN_TRADES = 5


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("spec", type=Path)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--candles", type=int, default=1000)
    source.add_argument("--ticks", type=Path)
    # A35: windows of the catalog (docs/scenarios.json); repeatable
    source.add_argument("--scenario", action="append")
    return parser.parse_args()


def _backtest(spec: Path, source: list[str]) -> dict:
    proc = subprocess.run(
        ["uv", "run", "--no-sync", "main.py", "backtest", str(spec), "--json", *source],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    stdout = proc.stdout.decode("utf-8", errors="replace")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        stderr = proc.stderr.decode("utf-8", errors="replace")
        return {"ok": False, "errors": [{"path": "", "msg": stderr[-2000:]}]}


def _source(args: argparse.Namespace) -> list[str]:
    if args.scenario:
        return [arg for name in args.scenario for arg in ("--scenario", name)]
    if args.ticks:
        return ["--ticks", str(args.ticks)]
    return ["--candles", str(args.candles)]


def _print_scenarios(result: dict) -> None:
    """One line per window (its return, drawdown, trades and flags), then the
    summary."""
    for row in result["windows"]:
        if "error" in row:
            print(f"{row['id']}: error: {row['error'][:200]}")
            continue
        r = row["result"]
        print(
            f"{row['id']}: return {r['return_pct']}%, drawdown "
            f"{r['max_drawdown_pct']}%, {r['closed_trades']} trades, pair "
            f"{row['stats']['return_pct']:+.1f}%, vs hold {row['vs_hold_pct']:+.1f}%, "
            f"flags {row['flags']}"
        )
    print("summary:", json.dumps(result["summary"], indent=2))


def main() -> int:
    # the Windows console is cp1252; Claude reads UTF-8 and nothing may crash
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    args = _parse_args()
    result = _backtest(args.spec, _source(args))
    if not result.get("ok"):
        print("backtest:", json.dumps(result, indent=2))
        return 1
    if args.scenario:
        _print_scenarios(result)
        return 0
    print("backtest:", json.dumps({k: result.get(k) for k in METRICS}, indent=2))
    trades = result.get("trades", [])
    print(f"trades ({len(trades)}, first {SHOWN_TRADES}):")
    for trade in trades[:SHOWN_TRADES]:
        print("  ", json.dumps(trade))
    return 0


if __name__ == "__main__":
    sys.exit(main())
