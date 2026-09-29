---
description: Implement the next (or given) roadmap phase from docs/agent-strategies.md, docs-first
argument-hint: "[phase number or name]"
---

Implement roadmap phase: $ARGUMENTS (if empty, the first phase not marked done
in the progress table).

1. **Read the plan.** Read `docs/agent-strategies.md` (phase list and progress
   table), then `docs/refactoring-backlog.md` for items scheduled into this
   phase, then `docs/architecture.md` for the current layout. Check the code
   for anything the docs claim that is no longer true.
2. **Plan in `docs/` first.** Write or refine the phase's concrete steps in
   `docs/agent-strategies.md` before touching code. The plan must live in the
   repo docs, never only in a plan file. If the scope is ambiguous, ask before
   implementing.
3. **Implement in small steps**, following `AGENTS.md`:
   - layering: add new modules to the map in `tests/test_architecture.py`;
   - ruff complexity is at most 5;
   - strategies use `self.clock()` / `self.rng`;
   - state paths go through `trader/paths.py`;
   - test file basenames are unique, and shared helpers go in `tests/factories.py`.
4. **Verify.** Run `uv run --no-sync python .claude/scripts/check.py` until it
   prints `ALL GREEN`. If the phase touches the run loop, execution or the
   ledger, also run `uv run --no-sync python .claude/scripts/smoke.py`.
5. **Close out** as in `/sync-docs`: mark the phase done in the progress table,
   update the backlog, `docs/architecture.md` and `AGENTS.md`. Then summarize
   what was built, what was deferred, and what the next phase is.
