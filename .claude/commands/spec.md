---
description: Validate and backtest a strategy spec, or author one from a description first
argument-hint: "<spec.json | description of the strategy> [--candles N | --ticks FILE]"
allowed-tools: Bash(uv run --no-sync python .claude/scripts/spec_check.py:*), Bash(uv run --no-sync main.py market:*), Bash(uv run --no-sync main.py strategy schema:*)
---

Input: $ARGUMENTS

**If it is a spec file**, validate and backtest it with the Bash tool:

```bash
uv run --no-sync python .claude/scripts/spec_check.py <spec.json> [--candles 1000 | --ticks FILE] [--refresh-expiry]
```

Validation rejects an `expires_at` in the past or more than 30 days ahead.
`--refresh-expiry` checks a copy with it set to now + 3 days. Use it for
stale or example specs (such as `docs/examples/spec-sol-dip.json`), and say
that you did.

**If it is a description**, author a spec first:
1. Get the contract from `uv run --no-sync main.py strategy schema` and look at
   `docs/examples/spec-sol-dip.json`.
2. Look at the market with `uv run --no-sync main.py market summary <SYMBOL> --interval 1_MINUTE`
   (plus `market price` / `market candles` if needed), so the thresholds fit
   current volatility.
3. Write the spec to the scratchpad (not the repo, unless asked). Then run
   `spec_check.py` on it and iterate until it validates.

Report the validation result and the backtest headline: return, max drawdown,
closed trades, win rate and the first trades. Point out when the backtest had
too few trades to mean anything. To see it trade live, suggest `/smoke --spec <file>`.
The agent commands are read-only; never write to the ledger to test a spec.
