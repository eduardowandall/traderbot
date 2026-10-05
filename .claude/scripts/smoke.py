"""Isolated smoke run in paper mode (`serve` + `connect`), then a ledger report.

State goes to a fresh temp dir (TRADER_DATA_DIR / TRADER_POLICY_FILE /
TRADER_LOG_DIR), with a permissive policy, so the real `.data/` ledger and
wallet are never touched. Only paper: `real` is deliberately not accepted.

Usage (from the project root):
    uv run --no-sync python .claude/scripts/smoke.py
        [--seconds 40] [--spec docs/examples/spec-random.json] [--seed 1]

Starts `serve paper` (the trade-runner), waits for its connection file, then
`connect`s a strategy spec (default: the random one) on the spec's own pair;
`--seed` fixes the `random_chance` draws. Trading is only `serve` + `connect`
(B14). The report comes from `ledger_dump.py` (the CLI has no ledger commands).
"""

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# o random opera quase a cada tick: mais folga que o padrão do paper
POLICY = "[paper.limits]\nmax_daily_notional_usd = 100000\nmax_trades_per_hour = 1000\n"
ERROR_MARKERS = ("ERROR", "Traceback")
MAIN = ["uv", "run", "--no-sync", "main.py"]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=int, default=40)
    parser.add_argument(
        "--spec",
        type=Path,
        default=ROOT / "docs" / "examples" / "spec-random.json",
        help="strategy spec JSON to run",
    )
    parser.add_argument("--seed", default="1", help="seed for random_chance")
    return parser.parse_args()


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


def _start(args: list[str], workdir: Path, name: str, env: dict) -> subprocess.Popen:
    print("$", " ".join(args), flush=True)
    with (
        open(workdir / f"{name}.out.txt", "wb") as out,
        open(workdir / f"{name}.err.txt", "wb") as err,
    ):
        return subprocess.Popen(args, cwd=ROOT, env=env, stdout=out, stderr=err)


def _wait_for(path: Path, server: subprocess.Popen, seconds: float = 60) -> bool:
    deadline = time.monotonic() + seconds
    while not path.exists():
        if server.poll() is not None or time.monotonic() > deadline:
            return False
        time.sleep(0.5)
    return True


def _connect(args: argparse.Namespace, workdir: Path, env: dict) -> subprocess.Popen:
    cmd = [*MAIN, "connect", str(args.spec), "--seed", args.seed]
    client = _start(cmd, workdir, "connect", env)
    time.sleep(args.seconds)
    if client.poll() is not None:
        print(f"[!] connect exited before {args.seconds}s (code {client.poll()})")
    return client


def _run_bot(args: argparse.Namespace, workdir: Path, env: dict) -> None:
    server = _start([*MAIN, "serve", "paper"], workdir, "serve", env)
    client = None
    try:
        if _wait_for(workdir / "data" / "trader-paper.json", server):
            client = _connect(args, workdir, env)
        else:
            print("[!] serve paper did not come up (see its log below)")
    finally:
        for proc in (client, server):
            if proc is not None:
                _stop(proc)


def _report_log(name: str, workdir: Path) -> None:
    path = workdir / f"{name}.err.txt"
    lines = _decode(path.read_bytes()).splitlines() if path.exists() else []
    errors = [line for line in lines if any(m in line for m in ERROR_MARKERS)]
    print(f"--- {name} stderr: {len(lines)} lines, {len(errors)} error lines")
    print("\n".join(errors[:10] or lines[-15:]))


def _report_ledger(env: dict) -> None:
    dump = [sys.executable, str(Path(__file__).with_name("ledger_dump.py"))]
    proc = subprocess.run(
        [*dump, "paper", "--limit", "8"], cwd=ROOT, env=env, capture_output=True
    )
    print(f"--- ledger\n{_decode(proc.stdout + proc.stderr).strip()}")


def main() -> int:
    # the Windows console is cp1252; Claude reads UTF-8 and nothing may crash
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    args = _parse_args()
    workdir = Path(tempfile.mkdtemp(prefix="tb-smoke-"))
    (workdir / "policy.toml").write_text(POLICY, encoding="utf-8")
    env = os.environ | {
        "TRADER_DATA_DIR": str(workdir / "data"),
        "TRADER_POLICY_FILE": str(workdir / "policy.toml"),
        "TRADER_LOG_DIR": str(workdir / "logs"),
        "PYTHONIOENCODING": "utf-8",
        "COLUMNS": "200",  # rich wraps the log at 80 columns otherwise
    }
    _run_bot(args, workdir, env)
    for name in ("serve", "connect"):
        _report_log(name, workdir)
    _report_ledger(env)
    print(f"--- state kept in {workdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
