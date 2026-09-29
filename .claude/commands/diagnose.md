---
description: Diagnose why a bot run isn't trading or looks wrong (denials, halts, UNCONFIRMED, bad PnL)
argument-hint: "<mode: paper|dry|real> [symptom]"
allowed-tools: Bash(uv run --no-sync main.py ledger list:*), Bash(uv run --no-sync main.py ledger verify:*), Bash(uv run --no-sync main.py pnl:*), Bash(uv run --no-sync main.py paper balance:*), Bash(uv run --no-sync main.py market price:*), Bash(ls:*), Bash(tail:*), Bash(grep:*)
---

Diagnose the run. Mode and symptom: $ARGUMENTS (the default mode is paper).

The checks below are all read-only. Don't run `ledger resolve`, `resume`,
`paper reset` or delete `.data/` files without asking; they change real
state. Work through the checks in order and stop at the first real cause:

1. **Ledger:** `uv run --no-sync main.py ledger list <mode> --limit 20`
   - `recusa:` lines mean the policy denied the intent. The usual paper case is
     `max_trade_usd` (25 by default) against sizing of 50-100% of the balance.
     Compare this with `policy.toml`: does `[<mode>.limits]` override
     `max_trade_usd`?
   - UNCONFIRMED intents block all trading until `main.py ledger resolve`.
   - `erro:` lines are execution failures.
2. **Halt:** check for a `HALT` file in the data dir (`$TRADER_DATA_DIR`, else
   `.data/`). A tripped circuit breaker needs `main.py resume <mode>`.
3. **Logs:** read the newest `.logs/<mode>-run-*.log`
   (`ls -t .logs | head -3`) and grep it for `ERROR|Traceback|denied|paus`.
   Remember that `denied`/`rejected` replies pause orders for 30s.
4. **Integrity and PnL:** run `ledger verify <mode>` and `pnl <mode>`. For
   paper, also run `paper balance`. If a PnL looks absurd (for example
   1000s of %), check the unit conversions (`ui_to_raw`/raw amounts) and
   whether costs are being subtracted twice. LP fees and slippage are already
   in the amounts.
5. **Market data:** if there are no ticks at all, check the feed with
   `uv run --no-sync main.py market price SOL`.

Report the cause with evidence (the ledger line or log line), then the fix. If
the fix is a code change, add a regression test. If it is a state action, give
the user the exact command.
