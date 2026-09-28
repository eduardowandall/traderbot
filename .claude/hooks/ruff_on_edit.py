"""PostToolUse hook: ruff format + ruff check --fix on edited .py files.

Reads the hook payload from stdin. Unfixable lint errors are fed back to
Claude via ``decision: block`` so they get fixed in the same turn.
"""

import json
import subprocess
import sys
from pathlib import Path


def _ruff(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ruff", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def main() -> None:
    payload = json.load(sys.stdin)
    file_path = (payload.get("tool_input") or {}).get("file_path") or ""
    path = Path(file_path)
    if path.suffix != ".py" or not path.is_file():
        return

    _ruff("format", "--quiet", str(path))
    check = _ruff("check", "--fix", "--quiet", "--output-format=concise", str(path))
    if check.returncode != 0:
        errors = (check.stdout + check.stderr).strip()
        print(
            json.dumps(
                {
                    "decision": "block",
                    "reason": f"ruff found unfixable issues in {path.name}:\n{errors}",
                }
            )
        )


if __name__ == "__main__":
    main()
