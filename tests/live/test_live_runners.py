"""B3 de ponta a ponta: `serve paper` + `connect` em processos separados."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from factories import example_spec

from trader.execution.ledger import Ledger, ledger_path
from trader.execution.models.intent import IntentStatus
from trader.shared.paths import PROJECT_ROOT, data_dir, policy_file

RUN_SECONDS = 35
POLICY = "[paper.limits]\nmax_daily_notional_usd = 100000\nmax_trades_per_hour = 1000\n"


def _start(*args: str) -> subprocess.Popen:
    env = os.environ | {"PYTHONIOENCODING": "utf-8"}
    return subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "main.py"), *args],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _stop(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], check=False)
    else:
        proc.kill()
    proc.wait()


def _busy_spec(tmp_path: Path) -> Path:
    spec = json.loads(Path(example_spec("random")).read_text(encoding="utf-8"))
    spec["entry"]["conditions"] = [{"type": "random_chance", "pct": 40}]
    spec["exit"]["conditions"] = [{"type": "random_chance", "pct": 20}]
    path = tmp_path / "busy.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


def _wait_for(path: Path, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        assert time.monotonic() < deadline, f"{path} não apareceu"
        time.sleep(0.5)


def test_a_strategy_runner_trades_through_a_paper_trade_runner(tmp_path):
    policy_file().write_text(POLICY, encoding="utf-8")
    server = _start("serve", "paper")
    try:
        _wait_for(data_dir() / "trader-paper.json")
        client = _start("connect", str(_busy_spec(tmp_path)), "--seed", "1")
        time.sleep(RUN_SECONDS)
        _stop(client)
    finally:
        _stop(server)

    with Ledger(ledger_path("paper")) as ledger:
        records = ledger.list_intents(100)
    executed = [r for r in records if r.status == IntentStatus.EXECUTED]
    assert executed, [(r.status, r.error, r.decision_reasons) for r in records]
    assert all(r.intent.account.startswith("paper:strategy:") for r in executed)
    assert all(r.intent.source == "serve:random" for r in executed)
    assert not [r for r in records if r.status == IntentStatus.UNCONFIRMED]
