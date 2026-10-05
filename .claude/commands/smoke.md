---
description: Smoke-run a spec in paper (an isolated serve + connect), then report both logs, the ledger and PnL
argument-hint: "[--seconds N] [--spec FILE] [--seed N]"
allowed-tools: Bash(uv run --no-sync python .claude/scripts/smoke.py:*)
---

Run a live smoke test with the Bash tool, from the project root:

```bash
uv run --no-sync python .claude/scripts/smoke.py $ARGUMENTS
```

- Defaults: `docs/examples/spec-random.json` with `seed=1`, for 40s, in paper.
- It starts `serve paper`, waits for its connection file, then runs
  `connect` for the spec (trading is only `serve` + `connect`, B14), and
  reports each process's stderr.
- State lives in a fresh `tb-smoke-*` temp dir with a permissive policy, so the
  real `.data/` ledger and paper wallet are never touched. The path is printed
  at the end.
- `--spec FILE` runs another strategy spec (every strategy is a spec) on its
  own symbol.
- Paper only. **Never trade in real mode from here.** A real trade is something
  the user runs themselves.

Give the timeout at least `--seconds` + 90s. After the run, report:
- the error-line count (plus any tracebacks);
- whether orders were executed, denied or left unresolved (the `ledger` dump);
- net PnL and the costs from the dump's `pnl` block.

A strategy spec with 0 trades over a short window is normal while its
conditions aren't met; say so rather than calling it a failure.
