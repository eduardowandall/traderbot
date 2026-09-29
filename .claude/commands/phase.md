---
description: Implement the next (or given) roadmap item from docs/plan.md, docs-first
argument-hint: "[phase number or name]"
---

Implement roadmap item: $ARGUMENTS (e.g. `A7`, `R1`, `B1`; if empty, the first
item not marked done in the progress table of `docs/plan.md` §6).

1. **Read the plan.** Read `docs/plan.md` (the item, the order rationale in
   §5 and the progress table in §6), then `docs/architecture.md` for the
   current layout. Check the code
   for anything the docs claim that is no longer true.
2. **Plan in `docs/` first.** Write the item's design (a "Design" bullet
   under it) in `docs/plan.md` before touching code. The plan must live in the
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
5. **Close out** as in `/sync-docs`: mark the item done in the progress table
   (and update the score in §2 if a goal moved), update `docs/architecture.md`
   and `AGENTS.md`. If you checked anything live, add it to `tests/live/`. Then
   summarize what was built, what was deferred, and what the next item is.
