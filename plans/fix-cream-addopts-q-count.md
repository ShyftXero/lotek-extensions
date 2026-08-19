# Plan: fix/cream-addopts-q-count

- **Branch:** `fix/cream-addopts-q-count`  (worktree: `.claude/worktrees/cream-addopts-q`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose
Fix #56: `cream/pyproject.toml` sets `addopts = "-q"`, so a caller's habitual `pytest -q`
compounds to `-qq`, at which pytest drops the `N passed in Xs` summary line on a green run.
It does not swallow failures (FAILED lines and exit 1 still print), so this is a green-path
reporting defect only — but the PR gate needs exact counts, and `-qq` hides them.

## Done
- [x] Replace `addopts = "-q"` with `addopts = "--tb=short"` in `cream/pyproject.toml`
- [x] Add `cream/tests/test_pytest_config.py` regression guard (asserts addopts contains
      neither `-q`, `-qq`, nor `--quiet`), proven RED before / GREEN after the toml edit

## Remaining
- [ ] Open PR, close out gate acks

## Notes / gotchas
- Scope is cream-only per the issue/unit hint. `scribble`, `registrar`, `vector` have the
  same `addopts = "-q"` pattern — intentionally left for a follow-up issue, not fixed here.
- Cross-repo rails gate caveat: the executing hook is the lotek primary tree's copy and may
  demand ext-inapplicable acks (e.g. `--ack-invariants`); documented known issue (ext#35).
