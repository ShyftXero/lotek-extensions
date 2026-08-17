# Plan: fix/scribble-softhostid-retrofit

- **Branch:** `fix/scribble-softhostid-retrofit`  (worktree: `.claude/worktrees/softhostid-retrofit`, off `main`)
- **PR:** not opened yet — branch is pushed; see "Remaining"
- **Status:** 🟢 ready to merge (work complete + verified; only the PR-open step is outstanding)

## Purpose

Prod (lotek.shyft.us) answered **500 to every** `POST /scribble/machine/engagements/<id>/findings` on
2026-08-15:

```
sqlalchemy.exc.ProgrammingError: (psycopg.errors.DatatypeMismatch)
column "source_finding_id" is of type integer but expression is of type character varying
```

`scribble_findings.source_finding_id` became a `SoftHostId` (TEXT-backed) when the model was fixed to
hold a core UUID — but that table already existed in prod, and `create_all` only ever ADDS columns; it
never retrofits an existing column's declared TYPE. So prod kept a native `INTEGER` column, and psycopg
binds the parameter at the type the *model* declares (`$4::VARCHAR`) — which Postgres refuses against an
integer column **even when the value is NULL**. Every insert into that table failed: minimal finding,
template instantiation, promote, dashboard, all of it. `SELECT count(*) FROM scribble_findings` on prod
was **0** — the feature had never once worked there.

CLAUDE.md already documented this trap and prescribed a manual `ALTER TABLE`. Nobody ran it for these
two columns, because "which columns are SoftHostId" was carried in someone's head. This branch makes
`create_all` do it.

## Done

- [x] `scribble/db.py` — `soft_host_id_columns_typed_integer(engine)`: reflection-driven detection of
      every `SoftHostId` model column the DB still stores as a native INTEGER. Split from the repair so
      the DETECTION is testable on SQLite. Reflection, not a hardcoded list — a column declared
      `SoftHostId` later is covered the day it is declared, which is precisely how `source_finding_id`
      and `asset_id` slipped through (only `Engagement.owner_id`/`.client_id` were ALTERed by hand).
- [x] `scribble/db.py` — `_widen_soft_host_id_columns(engine)`, called from `create_all` **before**
      `_remap_standalone_client_ids` (that function UPDATEs `client_id` with a host id, which under v2
      is a UUID — the exact write an unrepaired INTEGER column refuses). Postgres repaired; SQLite
      skipped (dynamic typing stores the value fine, and it cannot `ALTER COLUMN … TYPE` anyway); any
      other dialect warns rather than guessing at ALTER syntax. Idempotent.
- [x] `scribble/api_pat.py` — `lotek_finding_id` parsed with `_opt_host_id`, not `_opt_int`. A core
      finding id is a UUIDv7 under v2, so `int("0198…")` 400'd: **promoting a single scan finding was
      unreachable on every v2 host.** Same bug, same file, one helper away from the `client_id` fix that
      `_opt_host_id` was written for. `api_schemas.py` widened to `int | str | None` to match `client_id`'s
      convention so the OpenAPI/MCP schema stops advertising int-only.
- [x] Tests, each shown red before green:
      - `tests/test_db_additive_migration.py::test_detects_a_pre_existing_integer_soft_host_id_column`
      - `…::test_fresh_db_has_no_integer_soft_host_id_columns`
      - `…::test_create_all_widens_a_legacy_integer_column_on_postgres` — **the decisive one.** Gated on
        `SCRIBBLE_TEST_PG_URL`; rewinds one column to the pre-SoftHostId shape, asserts a *minimal*
        finding is refused, runs `create_all`, asserts it lands and a UUID round-trips as a `uuid.UUID`.
      - `tests/test_machine_engagements.py::test_promote_lotek_finding_accepts_a_uuid_core_id` — 201 and
        the re-post still dedups (which only holds if `SoftHostId` returns a `uuid.UUID`, not a string).
- [x] `tests/conftest.py` — `FakeFindingDTO.id` annotated `int | uuid.UUID`, so the harness is no kinder
      than the real host.

## Remaining

- [ ] **Open the PR.** The branch is pushed. `gh pr create` is intercepted by *lotek's* rails gate, which
      fires from `$CLAUDE_PROJECT_DIR` even for a PR in this repo, and demands `--ack-tests` /
      `--ack-adversarial` / `--ack-review` / `--ack-invariants` — markers that bind to lotek's HEAD and
      lotek's invariant suite, neither of which means anything for a change that lives here. This repo's
      own `rails_gate.py` has no ack flags at all. `RAILS_OVERRIDE=1` is the documented escape and was
      refused by the session's safety classifier, so the PR open is left to a human:
      ```sh
      GH_TOKEN=$(python3 scripts/gh-app-token.py | tail -1) gh pr create \
        --repo ShyftXero/lotek-extensions --base main --head fix/scribble-softhostid-retrofit \
        --title "fix(scribble): create_all widens a SoftHostId column a pre-existing DB stores as INTEGER"
      ```
      Reviews were done, they just could not be *recorded* in a gate that belongs to the other repo:
      security (DDL is interpolated only from model-declared, quoted identifiers — never reflection
      output or request data; no authz surface changes; input widening stays bounded to int-or-UUID) and
      adversarial (which is what caught the unmount-on-failure flaw now fixed in 720f142).
- [ ] Re-pin in lotek (`pyproject.toml` `[tool.uv.sources]` tag bump + `uv lock --upgrade-package
      scribble`), run the mounted tests, PR into lotek `main`, then cut a prod release tag.

## Notes / gotchas

- **Prod was already unblocked by hand** on 2026-08-15 (`ALTER TABLE scribble_findings ALTER COLUMN
  source_finding_id / asset_id TYPE VARCHAR(64)`), verified by a minimal finding returning 201. This
  branch is the durable fix — it is what repairs every OTHER pre-existing Postgres instance, and what
  stops the next `SoftHostId` column from repeating the story.
- **SQLite proves nothing here.** The whole class of bug is invisible on SQLite; the full suite was green
  the entire time prod was down. Run the PG test with a throwaway container:
  ```sh
  docker run -d --name scribble-pgtest -e POSTGRES_PASSWORD=scribble -e POSTGRES_USER=scribble \
    -e POSTGRES_DB=scribble -p 55432:5432 postgres:16-alpine
  SCRIBBLE_TEST_PG_URL="postgresql+psycopg://scribble:scribble@127.0.0.1:55432/scribble" \
    uv run --extra dev --with "psycopg[binary]" python -m pytest tests/test_db_additive_migration.py
  ```
- **Pre-existing red on `main`, NOT from this branch:** `tests/test_skill.py` (9 failures) asserts on a
  `skill/scribble-report-refine/` directory that is not tracked in the repo at all. Suite result here:
  **601 passed, 9 failed**, all nine in that file.
- **Not fixed here, worth a decision:** scribble keys its own tables on sequential `Integer` PKs while
  core v2 is UUIDv7 throughout (cream/registrar/vector all use native `uuid`). So a scribble engagement
  id is `2`, is enumerable, and is a different identity from the core engagement UUID with no column
  linking them — a bot holding a core engagement UUID gets a 404 from every scribble route and has no
  way to discover the mapping.
