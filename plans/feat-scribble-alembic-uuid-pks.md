# Plan: feat/scribble-alembic-uuid-pks

- **Branch:** `feat/scribble-alembic-uuid-pks` — **stacked on `fix/scribble-softhostid-retrofit`**, not `main`
- **Issues:** lotek#335 (rule + sequencing) · lotek-extensions#36 (this implementation)
- **PR:** not opened yet · **Status:** 🟡 in progress — step 1 of 6 done

## Purpose

Owner direction (2026-08-16): every extension id is UUIDv7, no bare ints, to kill trivial IDOR **as a
class**. Scribble is the only non-compliant extension (22 `Integer` PK columns across 21 tables).
Breaking change explicitly authorized.

Decisions taken: **Alembic** (not a fourth bespoke migrator) · **hard-break the URLs** (no `legacy_id`,
no redirect shim) · **remap `jobs.promoted_ref_id`** so job→report links survive.

## Why stacked, not off `main`

`fix/scribble-softhostid-retrofit` modifies `scribble/db.py`'s migrator chain — the exact code Alembic
replaces. Branching off `main` would guarantee a conflict with finished, verified, unmerged work
(lotek-extensions#35). This branch therefore includes that branch's commits; **merge #35 first**, and
verify `baseRefName` before merging this one — a stacked PR merges into its base, not `main`.

## Done — step 1: Alembic owns Scribble's schema

- [x] `alembic>=1.13` dependency; `scribble/alembic.ini` with **no `sqlalchemy.url`** (the target is the
      host's engine, supplied at mount; a URL here could only ever point at the wrong database).
- [x] `scribble/migrations/env.py`:
      - **`version_table="scribble_alembic_version"`** — Scribble runs inside the host's database
        alongside core's own Alembic history. Sharing `alembic_version` would make each project read the
        other's revision id as an unknown head and refuse.
      - **`include_object` scoped to the `scribble_` prefix.** Without it, `--autogenerate` against a
        mounted database proposes `DROP TABLE jobs` — that is the *default* behaviour, not an edge case.
      - Offline (`--sql`) mode raises rather than generating SQL for a database it cannot identify.
      - `-x url=` for autogenerating against a scratch DB only.
- [x] Baseline revision `e17599b0880a` — a **frozen literal snapshot** of today's int-PK schema
      (21 tables, 8 indexes, 0 non-Scribble tables). Deliberately not a `create_all` call: that would
      build the *future* UUID shape on a fresh DB and then try to migrate it again.
- [x] `run_migrations(engine)` with the three-case adoption logic, and `create_all` kept as a
      back-compat alias so the host contract is unchanged.
- [x] `tests/test_alembic_adoption.py` — Postgres-gated, 3 passing.

## Two bugs found by verifying rather than trusting

1. **The first baseline was EMPTY and reported success.** `env.py` imported `db.Base` but never
   `scribble.models`, so `Base.metadata` had no tables and autogenerate emitted a revision with zero
   `create_table` calls while printing `... done`. Caught only by counting the output
   (`grep -c op.create_table` → 0). `env.py` now imports the models with a comment saying why.
2. **`NameError: name 'scribble' is not defined`** at migration time — autogenerate renders custom
   types fully-qualified (`scribble.db.SoftHostId`) but revision files don't import the module.
   Fixed in the baseline and in `script.py.mako` so every future revision carries it.

## Remaining

- [ ] **Step 2 — the UUID PK revision.** 22 PK columns, 22 FK edges. Add uuid column → backfill
      `uuid7()` → **emit `scribble_pk_migration_map` while old and new ids coexist** → repoint FKs →
      swap → drop int. Regenerate the seeds (63 vuln templates, 87 checklist items).
- [ ] **Step 3 — route converters + front end.** 48 `<int:…>` → `<uuid:…>`; 4 `_opt_int` sites; the 9
      `parseInt`/`Number` coercions, which return `NaN` on a UUID rather than raising — silent.
- [ ] **Step 4 — lotek-side `jobs.promoted_ref_id` remap** consuming the mapping table, then dropping it.
- [ ] **Step 5 — collapse `SoftHostId` → `Uuid`** (and core's `HostRefId`); both exist only to tolerate
      an int-keyed peer.
- [ ] **Step 6 — enforce**: `INV-INTEGRITY-05` + a PK guard in lotek, then tighten `_ACCEPTED_TYPES` to
      `{"Uuid"}`. **Last**, or `main` goes red for the whole migration.

## Notes / gotchas

- The legacy one-shots (`_rename_from_fraction`, `_additive_column_sync`, `_widen_soft_host_id_columns`,
  `_remap_standalone_client_ids`) are **kept, not deleted**: they are what brings a drifted pre-Alembic
  database to the baseline shape *before* it is stamped. Stamping a drifted schema yields a chain that
  believes a column exists when it does not — silent until the next revision runs. They become dead code
  once every deployment has adopted.
- Ordering in `run_migrations` is load-bearing: rename → additive sync → widen → remap → **stamp** →
  upgrade.
- Tests are Postgres-gated on purpose. SQLite would accept a wrong-typed column and prove nothing — the
  same blindness that hid the INV-INTEGRITY-03 prod outage for weeks.
