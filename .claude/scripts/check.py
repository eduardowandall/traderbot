"""Local CI gate: ruff check, ruff format --check, pyright, pytest.

Mirrors `.github/workflows/ci.yml` and prints one line per step, plus the
tail of the output for failing steps. Every step runs even if an earlier one
fails, so a single call shows everything that needs fixing.

Usage (from the project root):
    uv run --no-sync python .claude/scripts/check.py [--fix] [--no-tests] [PATH ...]

PATHs narrow ruff to those files; test PATHs (under tests/) narrow pytest.
pyright always runs on `.` so the result matches CI.
"""

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTEST_FLAGS = ["-q", "-p", "no:cacheprovider", "-W", "error::ResourceWarning"]
TAIL_LINES = 40
# the line each tool ends with (pyright may append a "new version" nag after it)
SUMMARY = re.compile(r"passed|failed|error|checks|formatted|reformatted", re.I)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", help="files/dirs to check")
    parser.add_argument("--fix", action="store_true", help="ruff --fix + format")
    parser.add_argument("--no-tests", action="store_true", help="skip pytest")
    return parser.parse_args()


def _test_targets(paths: list[str]) -> list[str]:
    tests = [p for p in paths if Path(p).as_posix().startswith("tests")]
    return tests or ["."]


def _steps(args: argparse.Namespace) -> list[tuple[str, list[str]]]:
    targets = args.paths or ["."]
    steps = []
    if args.fix:
        steps.append(("ruff fix", ["ruff", "check", "--fix", "--quiet", *targets]))
        steps.append(("ruff reformat", ["ruff", "format", "--quiet", *targets]))
    steps += [
        ("ruff check", ["ruff", "check", "--output-format=concise", *targets]),
        ("ruff format", ["ruff", "format", "--check", *targets]),
        ("pyright", ["pyright", "."]),
    ]
    if not args.no_tests:
        steps.append(("pytest", ["pytest", *_test_targets(args.paths), *PYTEST_FLAGS]))
    return steps


def _run(name: str, argv: list[str]) -> bool:
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", *argv],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lines = (proc.stdout + proc.stderr).strip().splitlines()
    status = "PASS" if proc.returncode == 0 else "FAIL"
    summary = next((line for line in reversed(lines) if SUMMARY.search(line)), "")
    print(f"{status} {name} ({time.monotonic() - start:.0f}s) {summary}")
    if proc.returncode != 0:
        print("\n".join(f"    {line}" for line in lines[-TAIL_LINES:]))
    return proc.returncode == 0


def main() -> int:
    # the Windows console is cp1252; Claude reads UTF-8 and nothing may crash
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    results = [_run(name, argv) for name, argv in _steps(_parse_args())]
    print("ALL GREEN" if all(results) else "FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
