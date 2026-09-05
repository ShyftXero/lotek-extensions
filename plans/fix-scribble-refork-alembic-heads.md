# Plan: fix/scribble-refork-alembic-heads

- **Branch:** `fix/scribble-refork-alembic-heads`  (worktree: `.claude/worktrees/alembic-refork`, off `main`)
- **PR:** ShyftXero/lotek-extensions `fix/scribble-refork-alembic-heads`
- **Status:** 🟢 ready to merge

## Purpose
`main` is broken for everyone: scribble's Alembic chain has TWO heads again, so `upgrade head` /
`stamp head` raise "Multiple heads are present" and every test that boots the app ERRORs. Add one no-op
merge revision to rejoin the DAG — the same fix as the earlier `a7f3b9c1d2e4` precedent.

Confirmed with alembic's own API (`ScriptDirectory.get_heads()`), not a regex:

```
HEADS: ['a7d2c4e6f810', 'e5a1c3d7b920']
  a7d2c4e6f810  EngagementFinding refs/metadata columns   down: a7f3b9c1d2e4   (PR #171)
  e5a1c3d7b920  lotek#623 strategic_recommendations       down: d3f5a7c9b1e2   (PR #180)
```

Both branches are rooted at `a7f3b9c1d2e4` (the previous merge revision):
`a7f3b9c1d2e4 → a7d2c4e6f810` (#171) and `a7f3b9c1d2e4 → a1b2c3d4e5f6` (#176) `→ d3f5a7c9b1e2`
(#178) `→ e5a1c3d7b920` (#180).

## Done
- [x] Plan committed first
- [x] Re-confirmed both heads via `ScriptDirectory.get_heads()`
- [x] No-op merge revision `b8e4d2f6a130` with `down_revision = ("a7d2c4e6f810", "e5a1c3d7b920")`
- [x] `get_heads()` returns exactly `['b8e4d2f6a130']`
- [x] `tests/test_migration_single_head.py` — RED before the fix commit, GREEN after (both junitxml'd)
- [x] Mass ERRORs gone: **full** scribble suite `1501 tests, 0 errors, 0 failures, 11 skipped` (694 s)
- [x] Gate markers earned

## Remaining
- [ ] Merge (done by an independent check, not here)

## Notes / gotchas
- **Why this keeps happening.** Parallel branches each cut a revision off the same parent and merge
  minutes apart — #176/#178/#180 all landed inside ~30 s at 14:39 UTC on 2026-09-05, and #171 had
  already forked the day before. Neither side rebased onto the other, so both stayed rooted at
  `a7f3b9c1d2e4`. `tests/test_migration_single_head.py` is the guard, but it can only fire AFTER the
  fork exists — on `main`, post-merge. It is a detector, not a preventer.
- The merge is a genuine **no-op**: every parent revision already ADDed its own columns/tables; this
  revision only rejoins the DAG so `head` is singular again. Mirrors `a7f3b9c1d2e4`'s shape exactly.
- No `create_all` retrofit concern: nothing about column types changes here.
- **No new test.** `tests/test_migration_single_head.py` already IS the fail-before/pass-after test for
  this class; adding a second one would duplicate it. Evidence: `r5-before.xml` (`failures=1`) vs
  `r5-fork-guard.xml` (`failures=0`).
- A DB sitting on only one of the two old heads upgrades correctly — alembic walks the DAG and applies
  the *other* branch's unapplied revisions before the merge point. In practice no DB can be on a single
  head anyway: multi-head made `upgrade`/`stamp` refuse outright.
