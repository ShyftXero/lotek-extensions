# Plan: fix/scribble-merge-migration-heads

- **Branch:** `fix/scribble-merge-migration-heads`  (worktree: `.claude/worktrees/scribble-merge-heads`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose
Scribble `main` has TWO Alembic heads: `f0a1c2d3e4b5` (lotek#620 `risk_override`, PR #168) and
`f4c9a1b2e370` (lotek#617 `source_facts` superset, PR #167) both branched off `76a1de5a7c83` and merged
independently. Alembic refuses to `stamp('head')` / `upgrade head` with multiple heads, so
`scribble.db.run_migrations` raises and the extension **silently fails to mount** in lotek (discovery
swallows the exception). This branch rejoins the DAG with a no-op **merge revision** and adds a hermetic
single-head guard so a fork fails at the cheap end next time.

## Done
- [x] Confirmed exactly two heads (`f0a1c2d3e4b5`, `f4c9a1b2e370`) off `76a1de5a7c83`.

## Remaining
- [ ] Merge revision `a7f3b9c1d2e4` (`down_revision = (f0a1c2d3e4b5, f4c9a1b2e370)`), no-op up/down.
- [ ] Guard test: the migration script directory has exactly one head (red with two heads, green after).
- [ ] Cut a new tag on merge; lotek re-pins to it (the `chore/scribble-repin-620` core branch).

## Notes / gotchas
- No-op merge is correct: each parent already ADDed its own columns idempotently; this only linearizes
  the DAG. `ScriptDirectory(...).get_heads()` returns `['a7f3b9c1d2e4']` after.
- This is what blocked pinning lotek to the current scribble tag (`v2026.9.4.135325+g3dace13` won't
  mount); the core re-pin waits on the tag this branch produces.
