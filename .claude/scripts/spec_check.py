"""Validate a strategy spec and backtest it, printing only the key metrics.

Wraps the read-only agent commands `strategy validate` and `strategy backtest`
(JSON on stdout) and trims the backtest down to its headline numbers plus the
first few trades.

Usage (from the project root):
    uv run --no-sync python .claude/scripts/spec_check.py SPEC.json
        [--mode paper] [--candles 1000 | --ticks FILE] [--refresh-expiry]
"""

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
METRICS = (
    "ticks",
    "final_equity",
    "return_pct",
    "max_drawdown_pct",
    "closed_trades",
    "win_rate_pct",
)
SHOWN_TRADES = 5


def refresh_expiry(spec_path: Path, workdir: Path) -> Path:
    """Copy of the spec with `expires_at` moved to 3 days from now.

    Validation rejects expiries in the past or more than 30 days ahead, which
    stale or example specs (the docs one says 2099) trip over.
    """
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    if "expires_at" in spec:
        expires = datetime.now(UTC).replace(microsecond=0) + timedelta(days=3)
        spec["expires_at"] = expires.isoformat().replace("+00:00", "Z")
    path = workdir / spec_path.name
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("spec", type=Path)
    parser.add_argument("--mode", default="paper", help="mode for the policy check")
    parser.add_argument(
        "--refresh-expiry",
        action="store_true",
        help="check a copy whose expires_at is now + 3 days",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--candles", type=int, default=1000)
    source.add_argument("--ticks", type=Path)
    return parser.parse_args()


def _agent(*cli: str) -> dict:
    proc = subprocess.run(
        ["uv", "run", "--no-sync", "main.py", "strategy", *cli],
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


def _backtest_args(args: argparse.Namespace) -> list[str]:
    if args.ticks:
        return ["--ticks", str(args.ticks)]
    return ["--candles", str(args.candles)]


def main() -> int:
    # the Windows console is cp1252; Claude reads UTF-8 and nothing may crash
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    args = _parse_args()
    if args.refresh_expiry:
        args.spec = refresh_expiry(args.spec, Path(tempfile.mkdtemp(prefix="tb-spec-")))
    validation = _agent("validate", str(args.spec), "--mode", args.mode)
    print("validate:", json.dumps(validation, indent=2))
    if not validation.get("ok"):
        return 1
    result = _agent("backtest", str(args.spec), *_backtest_args(args))
    if not result.get("ok"):
        print("backtest:", json.dumps(result, indent=2))
        return 1
    print("backtest:", json.dumps({k: result.get(k) for k in METRICS}, indent=2))
    trades = result.get("trades", [])
    print(f"trades ({len(trades)}, first {SHOWN_TRADES}):")
    for trade in trades[:SHOWN_TRADES]:
        print("  ", json.dumps(trade))
    return 0


if __name__ == "__main__":
    sys.exit(main())
