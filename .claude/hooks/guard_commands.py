"""PreToolUse hook: keeps agent sessions away from real money and the key.

Blocks a Bash/PowerShell command that would start real mode, load or print
the key, point the policy elsewhere, or touch the real ledger or the real
trade-runner (its connection file holds the token, its lock the mode). Paper runs,
backtests, `ledger_dump.py` and resetting paper state stay allowed. This is a
guardrail for Claude Code sessions, not a sandbox: the owner runs real mode
from a terminal (see AGENTS.md).

`blocked(command)` is pure (tested in `tests/test_guard_commands.py`); `main`
reads the hook payload from stdin and denies with the reason.
"""

import json
import re
import shlex
import sys

# separadores de comandos: cada parte é conferida sozinha
_SEPARATORS = re.compile(r"[;&|\n]+")
# `.env`, mas não `.env.example`, `.venv` nem `os.environ`
_ENV_FILE = re.compile(r"(?<![\w.-])\.env(?![\w.-])")
_SECRET_VAR = re.compile(
    r"(\$\{?|\$env:|%)(SOLANA_PRIVATE_KEY|HELIUS_RPC_URL)\b", re.IGNORECASE
)
_ENV_DUMP = re.compile(r"\bprintenv\b|\benv:(?!\w)", re.IGNORECASE)

REASONS = {
    "real": (
        "modo real é do dono: rode `main.py run real` você mesmo num terminal "
        "(sessões de agente só usam paper e backtest)"
    ),
    "env_file": "--env-file carrega a chave da carteira; só o dono usa (modo real)",
    "policy_file": "TRADER_POLICY_FILE troca a política de risco; só o dono muda",
    "env": ".env guarda a chave da carteira; sessões de agente não leem nem mudam",
    "secret": "a chave e a URL do RPC não podem ir para a saída de um comando",
    "real_ledger": (
        "o ledger real é a trilha dos trades com dinheiro de verdade (e mover ou "
        "apagar libera intenções sem confirmação): só o dono mexe"
    ),
    "real_runner": (
        "trader-real.json tem o token do trade-runner real (quem o lê manda "
        "ordens com dinheiro de verdade): só o dono usa"
    ),
}

# regra -> teste sobre o comando inteiro
_TEXT_RULES = (
    ("env_file", lambda c: "--env-file" in c),
    ("policy_file", lambda c: "TRADER_POLICY_FILE" in c.upper()),
    ("real_ledger", lambda c: "ledger-real.sqlite3" in c.lower()),
    ("real_runner", lambda c: "trader-real" in c.lower()),
    ("secret", lambda c: bool(_SECRET_VAR.search(c) or _ENV_DUMP.search(c))),
    ("env", lambda c: bool(_ENV_FILE.search(c))),
)


def _tokens(part: str) -> list[str]:
    try:
        return shlex.split(part)
    except ValueError:  # aspas sem par: basta separar por espaço
        return part.split()


def _starts_real_mode(part: str) -> bool:
    """`... main.py ... run|serve ... real ...`, em qualquer ordem de opções."""
    tokens = [t.strip("'\"").lower() for t in _tokens(part)]
    for i, token in enumerate(tokens):
        if token.replace("\\", "/").endswith("main.py"):
            return _real_after_command(tokens[i + 1 :])
    return False


def _real_after_command(args: list[str]) -> bool:
    commands = [i for i, a in enumerate(args) if a in ("run", "serve")]
    return bool(commands) and "real" in args[commands[0] + 1 :]


def blocked(command: str) -> str | None:
    """O motivo para bloquear o comando, ou None se ele pode rodar."""
    for name, matches in _TEXT_RULES:
        if matches(command):
            return REASONS[name]
    if any(_starts_real_mode(part) for part in _SEPARATORS.split(command)):
        return REASONS["real"]
    return None


def main() -> None:
    payload = json.load(sys.stdin)
    command = (payload.get("tool_input") or {}).get("command") or ""
    reason = blocked(command)
    if reason is None:
        return
    decision = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": f"Bloqueado pelo guard do projeto: {reason}",
    }
    print(json.dumps({"hookSpecificOutput": decision}))


if __name__ == "__main__":
    main()
