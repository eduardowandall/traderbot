"""Isolated smoke run of the bot in paper or dry mode, then a ledger report.

State goes to a fresh temp dir (TRADER_DATA_DIR / TRADER_POLICY_FILE), with a
permissive policy, so the real `.data/` ledger, wallet and HALT file are never
touched. `real` mode is deliberately not accepted.

Usage (from the project root):
    uv run --no-sync python .claude/scripts/smoke.py [--mode paper|dry]
        [--seconds 40] [--symbol SOL-USDC] [--strategy random]
        [--args "buy_chance=100 sell_chance=100 seed=1"] [--spec FILE]

`--spec FILE` runs a strategy spec (its symbol wins, and `expires_at` is
moved to 3 days ahead so stale/example specs pass validation). `dry` loads `.env` and needs
SOLANA_PRIVATE_KEY + HELIUS_RPC_URL; it simulates the send, never trades.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from spec_check import refresh_expiry  # sibling script (its dir is on sys.path)

ROOT = Path(__file__).resolve().parents[2]
LIMITS = "max_trade_usd = 1000\nmax_daily_notional_usd = 100000\n"
POLICY = f"[paper.limits]\n{LIMITS}max_trades_per_hour = 1000\n\n[dry.limits]\n{LIMITS}"
ERROR_MARKERS = ("ERROR", "Traceback")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=["paper", "dry"], default="paper")
    parser.add_argument("--seconds", type=int, default=40)
    parser.add_argument("--symbol", default="SOL-USDC")
    parser.add_argument("--strategy", default="random")
    parser.add_argument("--args", default="buy_chance=100 sell_chance=100 seed=1")
    parser.add_argument("--spec", type=Path, help="strategy spec JSON to run")
    return parser.parse_args()


def _prepare_spec(args: argparse.Namespace, workdir: Path) -> None:
    path = refresh_expiry(args.spec, workdir)
    symbol = json.loads(path.read_text(encoding="utf-8"))["symbol"]
    args.symbol, args.strategy, args.args = symbol, "spec", f"file={path}"


def _uv(mode: str, *cli: str) -> list[str]:
    env_file = ["--env-file", ".env"] if mode == "dry" else []
    return ["uv", "run", "--no-sync", *env_file, "main.py", *cli]


def _decode(raw: bytes) -> str:
    # typer/click still write the Windows console codepage when redirected
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _stop(proc: subprocess.Popen) -> None:
    # SIGINT doesn't reach the bot on Windows; kill the whole uv process tree
    if os.name == "nt":
        taskkill = ["taskkill", "/PID", str(proc.pid), "/T", "/F"]
        subprocess.run(taskkill, capture_output=True, check=False)
    else:
        proc.kill()
    proc.wait()


def _run_bot(args: argparse.Namespace, workdir: Path, env: dict) -> str:
    cmd = _uv(args.mode, "run", args.mode, args.symbol, args.strategy, args.args)
    print("$", " ".join(cmd), flush=True)
    err_path = workdir / "err.txt"
    with open(workdir / "out.txt", "wb") as out, open(err_path, "wb") as err:
        proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=out, stderr=err)
        time.sleep(args.seconds)
        exited = proc.poll()
        _stop(proc)
    if exited is not None:
        print(f"[!] bot exited on its own before {args.seconds}s (code {exited})")
    return _decode(err_path.read_bytes())


def _report_log(log: str) -> None:
    lines = log.splitlines()
    errors = [line for line in lines if any(m in line for m in ERROR_MARKERS)]
    print(f"--- stderr: {len(lines)} lines, {len(errors)} error lines")
    print("\n".join(errors[:10] or lines[-15:]))


def _report_ledger(mode: str, env: dict) -> None:
    reports = [("ledger list", "ledger", "list", mode, "--limit", "8")]
    reports += [("pnl", "pnl", mode), ("ledger verify", "ledger", "verify", mode)]
    if mode == "paper":
        reports.append(("paper balance", "paper", "balance"))
    for title, *cli in reports:
        proc = subprocess.run(
            _uv(mode, *cli), cwd=ROOT, env=env, capture_output=True, check=False
        )
        output = _decode(proc.stdout + proc.stderr)
        print(f"--- {title}\n{output.strip()}")


def main() -> int:
    # the Windows console is cp1252; Claude reads UTF-8 and nothing may crash
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    args = _parse_args()
    workdir = Path(tempfile.mkdtemp(prefix="tb-smoke-"))
    (workdir / "policy.toml").write_text(POLICY, encoding="utf-8")
    env = os.environ | {
        "TRADER_DATA_DIR": str(workdir / "data"),
        "TRADER_POLICY_FILE": str(workdir / "policy.toml"),
        "PYTHONIOENCODING": "utf-8",
        "COLUMNS": "200",  # rich wraps the log at 80 columns otherwise
    }
    if args.spec:
        _prepare_spec(args, workdir)
    _report_log(_run_bot(args, workdir, env))
    _report_ledger(args.mode, env)
    print(f"--- state kept in {workdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
