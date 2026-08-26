# Plan: fix/scribble-machine-attackpath-contract

- **Branch:** `fix/scribble-machine-attackpath-contract`  (worktree: `.claude/worktrees/machine-api-contract`, off `origin/main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose

Two reporter-filed bugs on the SAME surface — scribble's PAT machine API — so one branch:

- **#114 (BUG-6, primary)** — `POST /scribble/machine/engagements/<id>/attack-paths` created a
  duplicate row for a retry carrying an identical `Idempotency-Key`, and the collection was
  append-only (no per-item `GET`/`PATCH`/`DELETE`), so the duplicate could not be undone over the
  API. Two rows = the diagram renders twice in the deliverable.
- **#116 (BUG-8)** — the machine API's response shapes are undocumented (no OpenAPI document served
  by the extension) and two collection keys do not match their route names (`attack-paths` returns
  `diagrams`; `findings` are nested inside `groups` and carry `id`, not the `finding_id` the write
  side accepts).

## Root cause (#114) — NOT specific to attack-paths

`_with_idempotency` (api_pat.py) delegates to the host seam `extras["idempotent"]`
(lotek `src/app/idempotency.py`). That seam only MEMOIZES a response it can store, and it decides
that with `_storable(body)` → `json.dumps(body)`. Since scribble's UUIDv7 migration (#36/lotek#335)
every machine-API response body carries raw `uuid.UUID` objects (`{"id": d.id, …}`), and
`json.dumps` raises `TypeError` on a `UUID`. The seam therefore takes its "not storable" branch,
**deletes the claim** ("release the claim so a retry re-executes"), and the next retry gets a fresh
claim and runs the mutation again.

So `Idempotency-Key` has been a silent no-op for **every** scribble machine route since the UUID
migration — attack-paths is just where it corrupted a deliverable. Artifact upload was unaffected
because it never uses `_with_idempotency`: it does its own durable
`(engagement_id, idempotency_key)` row lookup. That is the working reference the issue points at.

The fix is one JSON-normalisation in the shared helper, not a per-route dedup column: every
collection is repaired by the same five lines, and the wire format does not change (Flask's JSON
provider already renders a `UUID` as its `str`).

## Evals

Graders, deterministic, declared before the code:

| # | Grader | Scope | Baseline (origin/main) | Target |
|---|---|---|---|---|
| E1 | `pytest tests/test_report_attack_path.py` — double POST, same `Idempotency-Key` → `GET …/attack-paths` `count` | scribble | `count == 2` (duplicate) | `count == 1`, same `id` echoed |
| E2 | same file — `DELETE …/attack-paths/<ap_id>` → count | scribble | `404` (no route) | `200`, count drops to `0` |
| E3 | idempotency replay across the OTHER machine collections (groups) | scribble | duplicate rows | replay |
| E4 | `GET /scribble/machine/openapi.json` | scribble | `404` | `200`, valid OpenAPI 3.1 with a response schema per machine route |
| E5 | drift guard: every `machine_bp` route appears in the published document | scribble | n/a (no document) | green, fails if a route is added without a documented response |
| E6 | full scribble suite | scribble | record counts | no regression |

Harness note (INV-worthy): scribble's own conftest stub host published **no** `idempotent` extra, so
`_with_idempotency` fell through to `produce()` and the extension suite could never have caught this.
The stub gains a faithful port of lotek's `make_idempotent` — including the `json.dumps`-storability
branch that is the actual defect — so the harness is no kinder than production.

## Done

- [ ] filled in as work lands

## Remaining

- [ ] E1–E6

## Notes / gotchas

- **`diagrams` → `attack_paths` is a breaking rename.** Both keys are emitted for one release; the
  old one is documented as deprecated in `scribble/docs/SCRIBBLE.md` and in the PR body (this repo
  has no `CHANGELOG.md` — releases are auto-tagged, see `plans/chore-release-autonotes.md`).
- **Deliberately NOT done:** a flat top-level `findings` key on `GET …/findings`. The issue offers
  "rename OR document the nesting"; duplicating every finding into a second list doubles the
  response for a large engagement, and the published OpenAPI document now describes the nesting
  precisely. `finding_id` IS added as a read alias (issue's fix #3).
- **Deliberately NOT done:** rejecting a second POST with the same `diagram_ref` (issue's fix #3,
  "would be a reasonable additional guard"). Re-linking a re-exported snapshot of the same vector
  diagram is legitimate; with `Idempotency-Key` honoured the retry case is already covered.
- Standalone scribble (no mounting host) still has no idempotency store — `_with_idempotency` fails
  open exactly as every other host seam in this package does. Production is always mounted.
