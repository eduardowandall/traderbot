---
description: Run the local CI gate (ruff check, ruff format --check, pyright, pytest) and fix what fails
argument-hint: "[--fix] [--no-tests] [paths...]"
allowed-tools: Bash(uv run --no-sync python .claude/scripts/check.py:*)
---

Run the CI gate with the Bash tool, from the project root:

```bash
uv run --no-sync python .claude/scripts/check.py $ARGUMENTS
```

It mirrors `.github/workflows/ci.yml` (pytest also runs with `-W error::ResourceWarning`):
one `PASS`/`FAIL` line per step, and the output tail of each failing step.
Paths narrow ruff; test paths (under `tests/`) narrow pytest; pyright always runs on `.`.

If anything fails:
1. Fix the root cause. Don't add `# noqa`, `# type: ignore` or skips to get green.
   A mccabe `C901` means splitting the function or using table-driven dispatch,
   because `max-complexity = 5`.
2. Rerun only the failing scope (`check.py --no-tests <file>` or `check.py tests/<file>`),
   then do one last full `check.py` run.
3. Report the final line (`ALL GREEN` or what's still failing), with the test count.
