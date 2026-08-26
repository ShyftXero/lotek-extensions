# Plan: feat/bugreport-extension

- **Branch:** `feat/bugreport-extension`  (worktree: `.claude/worktrees/bugreport`, off `origin/main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress
- **Issue:** ShyftXero/lotek-extensions#112

## Purpose

A generic, **text-only** way for agents and users to file "here is an aspect of a bug". Capture only —
it does not file into GitHub or anywhere else. Users CRUD their **own** reports; admins CRUD all; and
whatever an admin does to a report (acknowledged / resolved / deleted) is visible **to the reporter**.

Deliberately the smallest extension in the repo. One table, one page, one machine API.

## Design

**One table, `bugreport_reports`** (prefix per the `[db]` manifest block):

| column | type | why |
|---|---|---|
| `id` | `Uuid` PK, `uuid.uuid7()` | v2 surrogate-PK convention (`db.UuidPk`) |
| `reporter_id` | `Uuid` null, indexed | **soft ref to a core `User`** — `Uuid`, never Integer/String (INV-INTEGRITY-03). No FK: the core table isn't known until mount |
| `reporter_name` | `String(120)` null | denormalised attribution — the row outlives the account |
| `title` | `String(200)` | text only |
| `body` | `Text` | text only |
| `status` | `Enum(ReportStatus)` indexed | `open` · `acknowledged` · `resolved` · `deleted` |
| `admin_note` | `Text` null | the feedback sentence the reporter reads |
| `created_at` / `updated_at` | `DateTime` | `db.TimestampMixin` |

**The tenancy axis is the reporter, not an engagement.** A bug report holds no client engagement data,
so it is one of INV-TENANCY-06's "row that legitimately has no engagement id" cases, and the invariant
requires such a row be gated by an **explicit declared platform rule** rather than default visibility.
The declared rule, in one place (`bugreport/service.py::visible_reports` / `::load_visible`):

> A report is visible to its reporter and to an admin. Nobody else, on any surface.

Per INV-TENANCY-01 a cross-user hit is **404, not 403** — a 403 would confirm the id exists.

**Admin delete is a tombstone** (`status=deleted` + `admin_note`), because a hard delete cannot tell the
reporter their report was deleted, which is exactly what the issue asks for. A reporter's delete of
their **own** report is a real `DELETE` — there is nobody to notify.

**Admin actions are audited** through the host seam in the same transaction (INV-AUDIT-03),
`ext:bugreport:admin_update`. Self-CRUD on your own row is not audited.

**Schema:** `create_all` + registrar's additive ADD-COLUMN pass, like `cream`/`registrar`/`vector`.
No Alembic — no extension in this repo owns an Alembic tree (scribble's revisions are core-side), and
this table's first migration is its creation.

## Evals

New extension ⇒ **the baseline is "the feature does not exist on `origin/main`"**; a before/after
outcome-set diff has nothing to diff. So the graders are the guards themselves, each measured by
neutralising it and watching the matching test go red (transcripts recorded for `--ack-transcripts`):

| # | Grader (deterministic) | Baseline | Target |
|---|---|---|---|
| E1 | `tests/test_authz.py` — user B reads/edits/deletes user A's report by id, on **both** surfaces (browser + PAT) | n/a (new) | every attempt **404**, A's row unchanged |
| E2 | Neutralise the owner filter in `service.visible_reports`/`load_visible` (return the query unscoped) | — | E1 goes **red** on both surfaces; that is the proof the guard is load-bearing |
| E3 | `tests/test_ui.py` — reporter sees the admin's status + note; non-admin cannot reach the admin verb | n/a (new) | pass; admin verb by a non-admin → 403 and no state change |
| E4 | `tests/test_machine_api.py` — PAT create/list/get/patch/delete, read-scope token refused on writes | n/a (new) | pass |
| E5 | `uvx ruff check bugreport` + `uv run --extra dev pyrefly check` + full `bugreport` suite | n/a (new) | clean / green |
| E6 | Sibling extensions untouched (`git diff --stat` names only `bugreport/` + `plans/`) | 4 subdirs green on `main` | unchanged — no cross-extension blast radius |

An LLM judge buys nothing here: every outcome is a status code or a row state.

## Done

- [x] Claim #112 (`status:todo → status:doing`, `### CLAIM` comment, bot token)
- [x] Worktree + split-identity git config
- [x] This plan

## Remaining

- [ ] `bugreport/` package (models, db, deps, service, blueprint, api_pat, host, templates)
- [ ] packaging: `pyproject.toml`, `lotek-extension.toml`, `README.md`, `docs/BUGREPORT.md`
- [ ] tests + red-then-green transcripts for E1/E2
- [ ] `/security-review` + `/adversarial-reviewer`, `--ack-*` markers, bot-authored PR

## Notes / gotchas

- **Deliberately NOT built** (not named in #112): GitHub/Jira filing, attachments, comment threads,
  labels/severity/priority, per-report assignment, search/pagination, email or in-app notification,
  an unread badge, a hard purge for admins, a cookie-authed browser JSON API (the UI is plain HTML
  form POSTs — no JS). Each is noted in the PR with when it would be worth adding.
- Local mount test trap: `lotek-extension.toml` is force-included at **wheel-build** time, so an
  `editable = true` path source silently skips it and the extension never mounts. Use a non-editable
  path source + `uv sync --reinstall-package bugreport`.
- `current_actor_is_admin()` treats *no hook at all* as standalone-admin and *a hook returning None*
  as anonymous → not admin. That asymmetry is the fail-closed line; do not "simplify" it away.
