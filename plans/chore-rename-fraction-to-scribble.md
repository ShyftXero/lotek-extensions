# Plan: chore/rename-fraction-to-scribble

- **Branch:** `chore/rename-fraction-to-scribble`  (worktree: `.claude/worktrees/rename-scribble`, off `main`)
- **PR:** to be opened bot-authored
- **Status:** 🟢 ready to merge

## Purpose
Rename the reporting extension **Fraction → Scribble** (the name never felt right; Fraction was a pun on
FACTION, the tool it seeds from). Repo identity `scribble`, url `/scribble`, tables `scribble_*`,
blueprint `scribble`, classes `Scribble*`, docs `docs/SCRIBBLE.md`. Mechanical — core does not dispatch
on the extension name, so this is a rename, not a refactor.

## Done
- [x] `git mv` the 5 `fraction`-named paths (dir, package, templates/, static/fraction.css, the
      `tests/test_fraction_report_authz.py`).
- [x] Content rename via a validated, case-sensitive 3-token sed (`Fraction/fraction/FRACTION →
      Scribble/scribble/SCRIBBLE`). **FACTION/faction (the external tool) preserved** — verified 0
      residual extension tokens, 70 FACTION refs intact, `faction_parse.py` untouched.
- [x] Data-preserving migration: `db._rename_from_fraction` — idempotent per-table
      `ALTER TABLE fraction_x RENAME TO scribble_x` for all **20** tables, at the top of `create_all`
      before `Base.metadata.create_all`. Test: `tests/test_rename_from_fraction.py`.
- [x] ruff clean.

## Remaining
- [ ] Extension suite green under `uv run`.
- [ ] Human approves + squash-merge.
- [ ] **lotek side (separate PR):** re-vendor `scribble`, `git rm extensions/fraction`, flip core
      `jobs.promoted_extension` 'fraction'→'scribble' in `migrations.py`, rename `docs/FRACTION.md`,
      update ~20 tests + comments, run-all-tests.

## Notes / gotchas
- The `jobs.promoted_extension` value flip is the HOST's (core owns `jobs`) — the extension renames only
  its own `scribble_*` tables. Handled in the lotek PR.
- Postgres `RENAME TO` carries FKs; index/sequence names keep old `fraction_` labels (cosmetic).
- lotek's heavy `rails_gate` cross-fires on monorepo `gh pr create` → opened with `RAILS_OVERRIDE=1`
  (monorepo's own gate + security-review satisfied).
