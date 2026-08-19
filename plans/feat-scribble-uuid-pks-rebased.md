# Plan: feat/scribble-uuid-pks-rebased

- **Branch:** `feat/scribble-uuid-pks-rebased` (worktree: `.claude/worktrees/scribble-uuid-pks-rebased`,
  off `origin/feat/scribble-alembic-uuid-pks`, merging in `origin/main`)
- **Issues:** lotek-extensions#36 (scribble Integer→UUIDv7 PKs) · lotek#355 (recovery/tracking) ·
  lotek#335 (rule + cross-repo sequencing)
- **PR:** DRAFT — do not merge without human review
- **Status:** 🟡 in progress — rebase in flight, overnight build-worker pass

## Purpose

`feat/scribble-alembic-uuid-pks` (base tip `27b1776`, "suite fully green — 618 passed") does the hard
part of #36: Alembic-owned schema, all 20 Scribble tables converted from `Integer` to `Uuid` PKs, routes/
parsers/tests updated, `scribble_pk_migration_map` emitted for the core-side remap. But that branch was
cut from a merge-base (`1a2e369`) that is now **6 commits behind `origin/main`** (tip `1c00281`). Those
6 commits (#58–#64) added new int-keyed surface the UUID branch never saw — most importantly a brand new
`findings_service.py` (`_coerce_int`, `exclude_id: int`) and ~54 `<int:...>` route converters across
files the base branch also touches. Merged forward naively, the stale branch would **revert** #58–#64.

This branch's job: merge `origin/main` into the UUID branch, resolve the textual conflicts, and
re-apply UUID handling to every semantically-conflicting int-keyed site that merged in clean (no textual
conflict, but still wrong-typed for the new PK contract). It explicitly does **not** attempt the
lotek-core companion (promoted_ref_id type change + remap) or the INV-INTEGRITY-05 enforcement guard —
those are separate, deliberately deferred (see Notes).

## Done
- [x] Fresh worktree cut off `origin/feat/scribble-alembic-uuid-pks`, never touching the locked
      `.claude/worktrees/scribble-uuid-pks` worktree/branch.
- [x] Verified premises against current `origin/main` before touching code: main tip `1c00281`, base
      tip `27b1776`, merge-base `1a2e369` — matches the plan exactly.

## Remaining
- [ ] Merge `origin/main` into this branch; resolve conflicts in `api_pat.py`, `db.py`,
      `engagement_ui.py`, `test_db_additive_migration.py`.
- [ ] Re-migrate `findings_service.py` (new on main) and every other `<int:...>`/`_coerce_int`/
      `_opt_int` site that merged in clean, to UUID.
- [ ] Front-end: audit the `parseInt`/`Number(...)` sites in the static JS for UUID handling.
- [ ] Confirm the Alembic revision needs no new-table work (table set unchanged main-vs-base) and
      re-run autogenerate against a scratch Postgres to confirm zero drift.
- [ ] Run the full Scribble suite under Postgres, including the Alembic/UUID-migration tests.
- [ ] Open DRAFT PR on `ShyftXero/lotek-extensions`, base `main`, flagging the core companion as a
      required-but-separate follow-up.

## Notes / gotchas

- **Do not touch** `.claude/worktrees/scribble-uuid-pks` (branch `feat/scribble-alembic-uuid-pks`) — it
  belongs to another session.
- **Core companion is bigger than the issue implies.** `lotek/src/app/models.py` still has
  `promoted_ref_id: Mapped[int | None] = mapped_column(Integer, ...)` and
  `lotek/src/app/host_contract.py` still does `int(ref_id)` — #328 only renamed the column, it never
  changed its type. That work (column type change + live-value remap consuming
  `scribble_pk_migration_map` + dropping the int cast) is a **separate lotek-core PR**, not part of this
  one, and must land before/with this PR merges or every promote breaks.
- **INV-INTEGRITY-05 enforcement is net-new**, not a tightening of an existing accepted-types set (no
  such set exists on current main) — deferred to land *after* the migration, never mid-migration.
- Tests are Postgres-gated on purpose (SQLite tolerates a wrong-typed column and proves nothing).
- Never run any migration in this branch against a live/prod database — scratch Postgres only.
