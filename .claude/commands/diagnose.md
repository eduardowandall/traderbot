---
description: Diagnose why a bot run isn't trading or looks wrong (denials, UNCONFIRMED, breaker, bad PnL)
argument-hint: "<mode: paper|real> [symptom]"
allowed-tools: Bash(uv run --no-sync python .claude/scripts/ledger_dump.py:*), Bash(ls:*), Bash(tail:*), Bash(grep:*)
---

Diagnose the run. Mode and symptom: $ARGUMENTS (the default mode is paper).

The checks below are all read-only. Don't move or delete `.data/` files
without asking: that is the only way to clear an UNCONFIRMED intent, and it
throws away the mode's history. Work through the checks in order and stop at
the first real cause:

1. **Ledger:** `uv run --no-sync python .claude/scripts/ledger_dump.py <mode> --limit 20`
   (set `TRADER_DATA_DIR` to read a `/smoke` run's state).
   - `status: denied` with `reasons` means the policy refused the intent;
     compare the limit in the reason with `policy.toml` (`[<mode>.limits]`).
   - A non-empty `unresolved` list (UNCONFIRMED, or EXECUTING for over 5
     minutes) blocks all trading in that mode. There is no resolve command:
     the owner checks the signature on the chain, then moves or deletes the
     ledger file.
   - `circuit breaker` in the reasons: consecutive failures in this process;
     restarting the bot re-arms it. Look at the `error` of the failed rows.
   - `error: "... formato antigo ..."`: a pre-stage-U ledger; it must be moved
     or deleted before the bot opens it.
2. **Logs:** read the newest `.logs/trader-*.log` (the log dir is `TRADER_LOG_DIR`,
   else `<project root>/.logs`; `ls -t .logs/trader-*.log | head -3`) and grep it
   for `ERROR|Traceback|denied|paus`. Remember that `denied`/`rejected` replies
   pause orders for 30s.
3. **PnL:** the `pnl` block of the dump, per account, and the `paper_wallet`
   balances. If a PnL looks absurd (for example 1000s of %), check the unit
   conversions (`ui_to_raw`/raw amounts) and whether costs are being subtracted
   twice. LP fees and slippage are already in the amounts.
4. **Market data:** if there are no ticks at all, the log shows the websocket
   and Price API fallback errors; `uv run pytest -m live` checks the feed.

Report the cause with evidence (the ledger row or log line), then the fix. If
the fix is a code change, add a regression test. If it is a state action, give
the user the exact step.
