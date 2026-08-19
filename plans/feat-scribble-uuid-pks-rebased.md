# Plan: feat/scribble-uuid-pks-rebased

- **Branch:** `feat/scribble-uuid-pks-rebased` (worktree: `.claude/worktrees/scribble-uuid-pks-rebased`,
  off `origin/feat/scribble-alembic-uuid-pks`, merging in `origin/main`)
- **Issues:** lotek-extensions#36 (scribble Integer→UUIDv7 PKs) · lotek#355 (recovery/tracking) ·
  lotek#335 (rule + cross-repo sequencing)
- **PR:** DRAFT — do not merge without human review
- **Status:** 🟢 rebase complete, suite green — ready for human review as a DRAFT (core companion still owed separately)

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

- [x] Merged `origin/main` into this branch; resolved conflicts in `api_pat.py`, `db.py`,
      `engagement_ui.py`, `test_db_additive_migration.py` — kept HEAD's Alembic-owned `run_migrations`
      and UUID route converters, kept main's new `include_in_report`/`_opt_str` upload validation and the
      view-cleanup `try/finally` robustness improvement.
- [x] Re-migrated `findings_service.py` (new on main — `_coerce_int` → `_coerce_uuid` via the shared
      `_as_uuid` parser, every `list[int]`/`set[int]` id-typed signature → `uuid.UUID`).
- [x] Re-migrated `api_pat.py`'s newly-clean-merged findings-CRUD block (#58–#64): 10 more
      `<int:...>` → `<uuid:...>` route converters, `_opt_int` → `_opt_uuid` for `group_id`/
      `assessment_type_id`, the bulk-move `finding_ids` int-coercion loop → `_as_uuid`-based, and removed
      the now-dead int-PK infra (`_ID` converter constant, `_INT_RE`, `_MAX_FINDING_ID`).
- [x] **Restored (not skipped) the strict `finding_id` validation main added** (adversarial review,
      2026-08-17): reverting straight to base's bare `_as_uuid()` at the upload route would have silently
      re-opened the exact bug main closed — a malformed `finding_id` landing as engagement-level evidence
      with `finding_id_dropped: false`. Added `_finding_id_or_400` (UUID-based version of main's
      int-based one) and wired it into both the multipart and JSON upload branches.
- [x] Front-end: audited every `parseInt`/`Number(...)` site in the static JS. The three UUID-relevant
      files (`artifacts.js`, `board.js`, `editor.js`) already carry a lotek#335 warning banner and no
      id-related coercion; `editor.js`'s two remaining `parseInt` calls are for a heading level and a
      generic numeric clamp, unrelated to ids. `checklists.js` has none.
- [x] Confirmed the Alembic revision needs no new-table work — table set is identical main-vs-base (no
      new tables from #60/#64); neither migration file was touched by the merge.
- [x] Ran the full Scribble suite against a scratch Postgres (`docker run postgres:16-alpine`, never a
      live/prod DB): **905 collected, 905 passed, 0 failed, 0 skipped.** `psycopg[binary]` had to be
      installed into the venv by hand — it is used by the Postgres-gated tests but is not declared as a
      project dependency anywhere (pre-existing gap, not introduced here — flagged below).
- [x] Fixed the int-era test debt the merge surfaced in the newly-landed suites (`test_machine_findings_crud.py`,
      `test_machine_client_onboarding.py`, `test_machine_artifacts.py`) — none of these files existed on
      the base branch, so they carried straight through from main with small-int id literals/fixtures that
      predate the PK migration:
      - id-returning fixtures now return `str(id)` (matching what JSON round-trips), with the handful of
        direct ORM-attribute comparisons (`child.group_id == gid`) explicitly `str(...)`-wrapped instead;
      - a literal int/out-of-range id standing in for "well-formed but nonexistent" was replaced with a
        real `uuid.uuid7()` (an int no longer parses as a `<uuid:...>` route segment at all, so it hit
        Werkzeug's own 404 page instead of the view's JSON 404 — a different assertion than intended);
      - `test_machine_client_onboarding.py`'s `Client(id=<int>)` fixture insert became a real UUID (the
        standalone `Client` table is UUID-keyed too);
      - the `test_a_finding_id_that_does_not_PARSE_is_refused_not_silently_dropped` parametrize list had
        one entry (a syntactically valid "core UUIDv7") that is well-formed under the new scheme and so
        is no longer a malformed-input case — moved to its own
        `test_a_well_formed_but_foreign_shaped_finding_id_is_dropped_not_refused`, and a couple of
        int-PK-era tests with no UUID analogue (`test_the_largest_id_the_column_holds_is_still_accepted`,
        the `int()`-coercion-specific `test_a_json_number_finding_id_does_not_attach_to_a_DIFFERENT_finding`)
        were removed rather than papered over, since the defect class they guarded against (silent
        numeric coercion) cannot occur through a UUID parser.
- [x] `ruff check .` and `pyrefly check .` both clean on the whole `scribble` subproject.
- [ ] Open DRAFT PR on `ShyftXero/lotek-extensions`, base `main`, flagging the core companion as a
      required-but-separate follow-up.

## Owed / follow-up (not in this PR)
- **lotek-core companion** (separate PR, see Notes below): `promoted_ref_id` type change + live-value
  remap, dropping the `int(ref_id)` cast.
- **`psycopg` is not a declared dependency** anywhere in `scribble/pyproject.toml`, yet the Postgres-gated
  migration tests require it (`postgresql+psycopg://`) — this predates this branch (base's own "618
  passed" claim was presumably run in an environment with it already installed some other way) but is
  worth fixing: add it to the `dev` extra so `uv sync --extra dev` is sufficient to run the full suite.
- **INV-INTEGRITY-05 PK guard + `_ACCEPTED_TYPES` tightening** (#335 step 6) — net-new, deferred to land
  after the core companion, never mid-migration.
- **SoftHostId → Uuid collapse** (#335 step 5) — deferred with the core companion.

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
