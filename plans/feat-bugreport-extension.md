# Plan: feat/bugreport-extension

- **Branch:** `feat/bugreport-extension`  (worktree: `.claude/worktrees/bugreport`, off `origin/main`)
- **PR:** not opened yet
- **Status:** 🟢 ready to merge
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
- [x] `bugreport/` package — models, db, deps, service, blueprint, api_pat, host, templates
- [x] packaging: `pyproject.toml`, `lotek-extension.toml`, `README.md`, `docs/BUGREPORT.md`
- [x] **62 tests green**, ruff clean, pyrefly 0 errors
- [x] E1/E2: 21 guards neutralised one at a time, each turning its own test red (below)
- [x] E5: validated against the **real** host parser — see "Mount proof"
- [x] `/adversarial-reviewer` → 4 warnings + 1 later type-confusion 500, all fixed with a
      red-then-green test each
- [x] `/security-review` → **no finding survived refutation**; 2 latent widening paths named and both
      closed (`bool(<bound method>)`, blueprint registration order). The same `bool()` shape in
      cream/registrar/vector is filed as #120 rather than folded into this branch

## Remaining

- [ ] `--ack-*` markers, bot-authored PR, issue #112 → `status:review`

## Eval results

**E2 — guard neutralisation (the deliverable, since a new extension has no baseline to diff).** Each
guard was reverted one at a time and the suite re-run; every one turned its own test red, and the two
tenancy predicates turned BOTH surfaces red at once, which is the property that proves they are one
decision and not two copies:

| mutation | result |
|---|---|
| `visible_reports` — drop the owner filter | **RED** 3 (browser list, machine list, unit) |
| `load_visible` — return the row regardless of owner | **RED** 10 (browser update/delete/respond, machine get/patch×2/delete, forged-uuid, anonymous, unit) |
| `_owns` — anyone owns anything | **RED** 11 |
| `admin_act` — drop the admin-only check | **RED** 2 (browser + machine) |
| `create` — allow an unattributable report | **RED** 2 |
| `update_own` — allow editing an admin-deleted report | **RED** 1 |
| `_clean` — drop title/length validation | **RED** 2 |
| `_require_write` — ignore the host's viewer gate | **RED** 1 |
| `current_actor_id` — accept a non-UUID host id | **RED** 1 |
| `current_actor_is_admin` — anonymous reads as admin | **RED** 1 |
| `api_pat._principal` — any PAT role reads as admin | **RED** 6 |
| `update_report` — let one call carry both a reporter edit and an admin response | **RED** 1 |
| `delete_report` — bypass `load_visible` | **RED** 1 |
| `admin_act` — blank the note on a status-only response (W1) | **RED** 1 |
| `visible_reports` — drop the `LIST_LIMIT` cap (W2) | **RED** 1 |
| `_owns` — drop the standalone arm (W3) | **RED** 1 |
| manifest: empty `[audit] verbs` / traversal in `machine_prefix` | **RED** 1 / 2 |
| `update_own` — let an admin rewrite someone else's text | **RED** 1 |
| `_text` — drop the non-string type guard | **RED** 7 (500s instead of 400s, both surfaces) |
| `current_actor_is_admin` — `bool(...)` instead of `is True` | **RED** 1 (a non-admin reads another user's report) |
| `register()` — browser blueprint registered before the machine one | **RED** 1 |
| `admin_act` — drop the status allow-list | **STILL GREEN** ⇒ the check was DEAD (`ReportStatus(...)` already raises); deleted rather than covered |

**E5** — `uvx ruff check bugreport` clean · `pyrefly check bugreport tests` 0 errors · 62 passed.
**Postgres** — SQLite is dynamically typed and hides INV-INTEGRITY-03 entirely, so the schema was also
round-tripped on a real PG 16: `id` and `reporter_id` both land as **native `uuid`**, and the tenancy
filter, the tombstone and full CRUD work there. `tests/test_manifest.py` guards the declared types so a
future `String`/`Integer` core-ref cannot land unnoticed.
**E6** — `git diff --stat` names only `bugreport/` and `plans/`; no sibling extension touched.

## Mount proof

Rather than assert against a mirror of the host's rules, the built wheel was installed into a scratch
venv and fed to lotek's **real** `app.extensions.discover_extensions()`:

```
discovered  : bugreport bugreport /bugreport
nav         : (NavEntry(label='Bug reports', path='/', icon='🐛'),)
machine url : /bugreport/machine/
docs        : docs/BUGREPORT.md | Bug reports (capture)
audit verbs : ('admin_update',) -> ('ext:bugreport:admin_update',)
db decl     : ExtensionSchema(base_ref='bugreport.models:Base', table_prefix='bugreport_', problems=())
```

That also proves the wheel really carries `lotek-extension.toml` inside the package dir (the
force-include trap: an `editable = true` path install skips the build and the extension silently
never mounts).

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
  `is_standalone()` logs a one-shot WARNING when it takes the standalone arm, because absence of the
  hook is the one input that WIDENS access and it would otherwise fail open mutely.
- **Owed at re-pin time, not here:** an extension's real contract is how it behaves MOUNTED, and a stub
  host proves logic, never the mount. When lotek pins this extension, land
  `lotek/tests/test_bugreport_extension.py` covering the cross-user 404 with a REAL `g.principal` — the
  seam gap that bit scribble (a PAT authenticating and then being refused every write) was invisible in
  the extension's own suite and only appeared mounted.
