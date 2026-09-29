---
description: Bring AGENTS.md and docs/ back in line with the current code changes
argument-hint: "[focus, e.g. a module or phase]"
allowed-tools: Bash(git status:*), Bash(git diff:*)
---

Sync the docs with the code. Focus: $ARGUMENTS (default: everything in the
working-tree diff).

1. Use `git status --short` and `git diff --stat` to see what changed. Read the
   diffs of the changed `trader/`, `main.py` and `tests/` files.
2. Update each doc only where the change affects it:
   - `AGENTS.md`: commands, architecture bullets, and known issues/quirks.
     Keep it terse; it is loaded into every session.
   - `docs/architecture.md`: the step-by-step tour, covering new or moved
     modules and changed call flows.
   - `docs/plan.md`: the **progress table** (§6), the score (§2), known
     issues (§7), and any new debt you noticed, added as an item in the stage
     where it belongs.
3. Remove anything the code no longer supports, such as deleted modules,
   renamed functions or dead commands. Grep for them:
   `grep -rn "<old name>" --include=*.md .`
4. Don't restate code in prose; link to the file (`trader/x.py`) instead.
   Report which docs changed and why, in one line each.
