"""B4: o hook que mantém sessões de agente longe do modo real e da chave."""

import importlib.util
import json
import subprocess
import sys

import pytest

from trader.shared.paths import PROJECT_ROOT

HOOK = PROJECT_ROOT / ".claude" / "hooks" / "guard_commands.py"
_spec = importlib.util.spec_from_file_location("guard_commands", HOOK)
assert _spec and _spec.loader
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


@pytest.mark.parametrize(
    "command",
    [
        "uv run main.py run real docs/examples/spec-sol-dip.json",
        "uv run --env-file .env main.py run real spec.json",
        "uv run main.py run --seed 1 real spec.json",
        'uv run main.py run "real" spec.json',
        "uv run --no-sync python main.py run REAL spec.json",
        r"uv run python C:\repo\main.py run real s.json",
        "cd x && uv run main.py run real s.json",
        "uv run --env-file other.env main.py run paper s.json",
        "TRADER_POLICY_FILE=/tmp/loose.toml uv run main.py run paper s.json",
        "$env:TRADER_POLICY_FILE = 'loose.toml'; uv run main.py run paper s.json",
        "cat .env",
        "Get-Content .\\.env",
        "type C:\\repo\\.env",
        "echo KEY=x >> .env",
        "grep HELIUS .env",
        "echo $SOLANA_PRIVATE_KEY",
        "echo ${HELIUS_RPC_URL}",
        "Write-Output $env:SOLANA_PRIVATE_KEY",
        "echo %HELIUS_RPC_URL%",
        "printenv",
        "Get-ChildItem Env:",
        "sqlite3 .data/ledger-real.sqlite3 'delete from intents'",
        "rm .data/ledger-real.sqlite3",
        "Remove-Item .data\\ledger-real.sqlite3",
        "mv .data/ledger-real.sqlite3 /tmp/",
        "cat .data/trader-real.json",
        "uv run main.py connect spec.json --trader .data/trader-real.json",
        "uv run main.py serve real",
    ],
)
def test_blocks_what_only_the_owner_does(command):
    assert guard.blocked(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "uv run main.py run paper docs/examples/spec-random.json --seed 1",
        "uv run main.py run paper docs/examples/real-ish.json",
        "uv run main.py backtest docs/examples/spec-sol-dip.json --json",
        "uv run --no-sync python .claude/scripts/ledger_dump.py real",
        "uv run --no-sync python .claude/scripts/smoke.py --seconds 40",
        "uv run pytest -m live",
        "cat .env.example",
        "cp .env.example /tmp/x",
        "ls .venv",
        "grep -rn os.environ trader",
        "grep -rn SOLANA_PRIVATE_KEY trader",
        "rm .data/ledger-paper.sqlite3 .data/paper-wallet.json",
        "git status",
        "echo real",
        "uv run main.py serve paper",
        "uv run main.py connect docs/examples/spec-sol-dip.json",
    ],
)
def test_allows_paper_backtests_and_development(command):
    assert guard.blocked(command) is None


def test_every_reason_is_plain_ascii_for_the_console():
    # o motivo aparece no Claude Code; texto Latin-1 cabe no console cp1252
    for reason in guard.REASONS.values():
        reason.encode("cp1252")


def _run_hook(command: str) -> str:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout


def test_the_hook_denies_with_the_reason():
    body = json.loads(_run_hook("uv run main.py run real s.json"))
    decision = body["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    assert "modo real" in decision["permissionDecisionReason"]


def test_the_hook_is_silent_when_allowed():
    assert _run_hook("uv run main.py run paper s.json") == ""


def test_settings_wire_the_hook_and_deny_the_key_files():
    settings = json.loads(
        (PROJECT_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    deny = set(settings["permissions"]["deny"])
    assert {"Read(./.env)", "Edit(./.env)", "Edit(./policy.toml)"} <= deny
    (entry,) = settings["hooks"]["PreToolUse"]
    assert entry["matcher"] == "Bash|PowerShell"
    assert "guard_commands.py" in entry["hooks"][0]["command"]
