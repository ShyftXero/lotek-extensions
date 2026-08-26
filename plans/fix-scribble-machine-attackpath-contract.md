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

- [x] **The shared idempotency fix** — `api_pat._with_idempotency` now normalises `produce()`'s body
      through the app's JSON provider (`_json_safe`) before handing it to the host seam. Five lines;
      repairs `Idempotency-Key` on **every** machine-API collection, not just attack-paths.
- [x] **Per-item attack-path routes** — `GET` / `PATCH` / `DELETE`
      `…/engagements/<eid>/attack-paths/<ap_id>`. `GET` adds the `embed_html` snapshot the listing
      omits; `PATCH` takes `include_in_report` + `caption` (the non-destructive unpublish); `DELETE`
      removes the row and RE-PACKS the survivors' `order_index` (it is a slot in the rendered list, so
      a hole would collide with the next link's `len(siblings)`).
- [x] **Tenancy** — the new routes carry the collection route's gate (`_visible_engagement`) plus
      `_diagram_of`, the diagram-shaped twin of `_group_of`: a diagram addressed through the WRONG
      engagement is the same 404 as one that never existed.
- [x] **`attack_paths` key** on the listing, with `diagrams` kept as a deprecated duplicate alias.
- [x] **`finding_id` read alias** on every finding payload (`_finding_summary`).
- [x] **`GET /scribble/machine/openapi.json`** — OpenAPI 3.1, `read` scope. Paths/params/scopes/request
      bodies introspected from the live `url_map` (so a new route cannot be absent); response schemas
      declared in `scribble/openapi.py::_RESPONSES`, drift-guarded.
- [x] **Test harness** — conftest now publishes a faithful `idempotent` extra (see below).
- [x] Docs: `scribble/docs/SCRIBBLE.md` — the three new routes, the discovery section rewritten around
      the published document, a "response shapes that have surprised clients" block, and a correction to
      a stale paragraph that still described machine path ids as bounded **integer** converters (they
      have been `<uuid:>` since #36).

## Evals — measured

| # | Baseline (`origin/main` behaviour) | After | Grader |
|---|---|---|---|
| E1 | 2 rows, 2 different UUIDs from one `Idempotency-Key` | `count == 1`, the SAME `id` echoed | `test_repeated_link_with_same_idempotency_key_creates_ONE_row` |
| E2 | `DELETE …/attack-paths/<id>` → 404 (no route) | 200, count drops to 0 | `test_delete_attack_path_drops_the_count_to_zero` |
| E3 | groups duplicated under one key | replay; key reuse for a different request → 422 | `test_the_idempotency_seam_is_honoured_across_the_machine_api` |
| E4 | `GET …/machine/openapi.json` → 404 | 200, OpenAPI 3.1, a response schema on all 31 routes | `tests/test_machine_openapi.py` |
| E5 | n/a | drift guard green; fails on a route with no declared response | `test_every_machine_route_is_documented_with_a_response_schema` |
| E6 | see PR body | see PR body | full `scribble` suite |

### Red→green transcripts (every guard neutralised, watched fail, restored)

| Guard | Red | Green |
|---|---|---|
| `_json_safe` in `_with_idempotency` | 2 failed | 2 passed |
| **harness fidelity**: stub `_storable` branch made kinder AND the bug restored | **1 passed — a FALSE green** | with the faithful stub: 1 failed → fix restored: 1 passed |
| `_diagram_of` engagement-ownership check | 1 failed, 17 passed | 18 passed |
| engagement gate on `DELETE …/attack-paths/<id>` (proves the tenancy SWEEP covers the new routes) | 1 failed, 16 passed | 17 passed |
| `order_index` re-pack on delete | 1 failed | 1 passed |
| `attack_paths` key | 3 failed, 14 passed | 17 passed |
| `finding_id` alias | 1 failed | 1 passed |
| an entry removed from `_RESPONSES` | 1 failed | 1 passed |
| the `/openapi.json` route renamed | 1 passed, 5 errors | 6 passed |

Worth recording from the second row: a stub that is KINDER than production would have hidden #114
completely — the acceptance test passes against the unfixed code as soon as the stub stops modelling the
`json.dumps`-storability branch. And from the fourth: the existing tenancy SWEEP does not catch a missing
`_diagram_of` ownership check (it never gets that far — the engagement gate refuses first), which is why
the cross-engagement case has its own dedicated test.

## Remaining

- [ ] reviews + acks + PR

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
