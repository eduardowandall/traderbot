---
description: Backtest a strategy spec, or author one from a description first
argument-hint: "<spec.json | description of the strategy> [--candles N | --ticks FILE]"
allowed-tools: Bash(uv run --no-sync python .claude/scripts/spec_check.py:*)
---

Input: $ARGUMENTS

**If it is a spec file**, backtest it with the Bash tool:

```bash
uv run --no-sync python .claude/scripts/spec_check.py <spec.json> [--candles 1000 | --ticks FILE]
```

The script runs `backtest --json`, which reports format errors
(`{"ok": false, "errors": [...]}`) and
refuses too few bars for the warm-up. The mode's policy limits (for example
`sizing.usd` above `max_trade_usd`) are checked only by `run`. Prefer
`"ttl_days": N` over a fixed `expires_at`, which `run` rejects when it is in
the past or more than 30 days ahead.

**If it is a description**, author a spec first:
1. Read `docs/specs.md`: it is the whole contract (fields, every condition type,
   how a spec trades, the limits `run` checks). Start from
   `docs/examples/spec-sol-dip.json`.
2. Write the spec file: in `docs/examples/spec-<name>.json` if the user wants
   it kept, otherwise in the scratchpad. Then run `spec_check.py` on it and
   iterate until it backtests.

Report the backtest headline: return, max drawdown, closed trades, win rate
and the first trades. Point out when the backtest had too few trades to mean
anything. To see it trade live, suggest `/smoke --spec <file>`. The backtest
is read-only; never write to the ledger to test a spec.
