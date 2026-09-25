# Plan: fix/scribble-dup-report-board-guard

- **Branch:** `fix/scribble-dup-report-board-guard` (worktree `.claude/worktrees/scribble-dup-guard`, off `origin/main`)
- **PR:** not opened yet
- **Status:** 🟢 ready — durable fix for the prod scribble-mount outage

## Purpose
Prod (release `v20260925040731+gd989516`) failed to mount scribble: the Report Board rename migration
`d1e2f3a4b5c6` builds a 1:1 partial-unique index on `scribble_report_boards.core_engagement_id`, but prod
held **two** boards linking one core engagement (the "TeamsPlus" pentest — 6 findings each), created via
the machine create path that inserted unconditionally. `CREATE UNIQUE INDEX` raised UniqueViolation, the
migration rolled back, scribble would not mount. (lotek#914 neighbourhood; the manual prod fix nulled one
link.)

Two defects, two fixes:

1. **Recurrence — the unguarded create path.** `api_pat.scribble_create_engagement` inserted a new
   `ReportBoard` even when one already linked that `core_engagement_id`. Now it **resolves-or-creates**
   (oldest-wins, returns the existing board with 200), mirroring the UI's `_board_for_core`. No second
   board can be made; no 500 from the index.
2. **Self-heal — the migration aborts on legacy dups.** `d1e2f3a4b5c6` now nulls the link on all but the
   oldest board per `core_engagement_id` BEFORE creating the index (dialect-agnostic; PG has no
   min(uuid)). An existing DB with dup links migrates cleanly instead of failing the mount; the newer
   duplicates survive as orphan boards with their findings intact.

## Evals
- **Capability:** `test_create_engagement_is_idempotent_on_core_engagement_id` (SQLite) — 2nd create
  returns the existing board, exactly one row. PASS.
- **Capability:** `test_rename_migration_deduplicates_core_engagement_links_then_builds_the_unique_index`
  (PG-gated) — a DB with two boards for one core id migrates green; oldest keeps the link, newer nulled,
  both rows survive, index built. PASS (verified on ephemeral PG).
- **Regression:** full `scribble` suite green.

## Done / Remaining
- [x] Guard the machine create path; self-heal the migration; two tests (both green).
- [ ] Reviews + acks, PR, merge (auto-tag), re-pin in lotek, new lotek release → prod.

## Notes
- The manual prod fix (nulling the newer "TeamsPlus" board's link) already restored the running prod
  instance; this fix prevents recurrence and heals any other un-migrated DB (dev / other installs).
