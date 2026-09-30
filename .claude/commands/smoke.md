---
description: Smoke-run the bot in paper (or dry) mode with isolated state, then report the log, ledger and PnL
argument-hint: "[--mode paper|dry] [--seconds N] [--spec FILE] [--seed N]"
allowed-tools: Bash(uv run --no-sync python .claude/scripts/smoke.py:*)
---

Run a live smoke test with the Bash tool, from the project root:

```bash
uv run --no-sync python .claude/scripts/smoke.py $ARGUMENTS
```

- Defaults: `paper`, `docs/examples/spec-random.json` with `seed=1`, for 40s.
- State lives in a fresh `tb-smoke-*` temp dir with a permissive policy, so the real
  `.data/` ledger, the paper wallet and `HALT` are never touched. The path is
  printed at the end.
- `--spec FILE` runs another strategy spec (every strategy is a spec). Its
  symbol is used, and `expires_at` is moved to 3 days ahead.
- `--mode dry` loads `.env` (real wallet, simulated send). The script refuses
  `real`. **Never trade with real mode from here.** A real trade is something the
  user runs themselves.

Give the timeout at least `--seconds` + 90s. After the run, report:
- the error-line count (plus any tracebacks);
- whether orders were executed, denied (`recusa:` lines) or stuck UNCONFIRMED;
- net PnL and the costs from `pnl`;
- whether `ledger verify` said the ledger is intact.

A strategy spec with 0 trades over a short window is normal while its
conditions aren't met; say so rather than calling it a failure.
