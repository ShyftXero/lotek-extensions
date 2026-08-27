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
| `include_in_report` boundary on PATCH | 1 failed | 1 passed |
| a request-model id retyped back to `integer` | 1 failed | 1 passed |
| an emitted audit verb dropped from the manifest | 1 failed | 1 passed |
| a declared audit verb nothing emits (false-promise direction) | 1 failed | 1 passed |
| retry refusals guessed from the HTTP method again | 1 failed | 1 passed |
| the non-object-body guard removed | 1 failed | 1 passed |
| the POST's audit row removed | 1 failed | 1 passed |
| the report route's media-type override removed | 1 failed | 1 passed |
| `400` dropped from the documented refusals | 1 failed | 1 passed |
| **harness fidelity, round 2**: the stub's in-flight `409` branch removed | 1 failed | 1 passed |

Worth recording from the second row: a stub that is KINDER than production would have hidden #114
completely — the acceptance test passes against the unfixed code as soon as the stub stops modelling the
`json.dumps`-storability branch. And from the fourth: the existing tenancy SWEEP does not catch a missing
`_diagram_of` ownership check (it never gets that far — the engagement gate refuses first), which is why
the cross-engagement case has its own dedicated test.

## Review findings and how each was resolved

Three independent reviewers (fresh agents, no authoring context) plus a `/security-review` pass, all on
the full branch diff, with lotek core's `INVARIANTS.md` cross-checked.

**`/security-review`: no findings at or above the bar.** It verified cross-tenant refusal empirically —
two principals, disjoint client grants, all six GET/PATCH/DELETE × wrong-engagement / wrong-client
combinations answered 404 with byte-identical bodies — and confirmed all 16 `_with_idempotency` call
sites authorize BEFORE the seam, so a replay cannot bypass tenancy.

| # | Finding | Resolution |
|---|---|---|
| CRITICAL | Request models declared `integer` ids on a UUID-keyed API (8 fields, stale since #36). A client generated from the document 400s on every call — the exact #116 failure, published with authority. | Retyped to `uuid.UUID`; host `SoftHostId`s (`client_id`, `core_engagement_id`, `lotek_finding_id`) deliberately left `int \| str`. New guard `test_request_model_ids_are_uuid_typed` walks the DOCUMENT, with a positive control that the SoftHostId allowlist cannot be padded. |
| CRITICAL | Two audit verbs emitted but not registered in `lotek-extension.toml [audit] verbs` — INV-AUDIT-03 (**active**) breach: the rows land but `/admin/audit`'s filter cannot select them. | Registered all three attack-path verbs; added `test_every_emitted_audit_verb_is_registered_in_the_manifest` **and** the reverse (a declared verb nothing emits is a false promise in the dropdown). |
| CRITICAL | The document promised `Idempotency-Key` on every mutating route; four do not honour it, including `promote-job`, which BULK-CREATES findings. `POST …/artifacts` has divergent semantics that silently keep only the first of two files under one key. | Added `IDEMPOTENT_ATTR` (same mechanism as `SCOPE_ATTR`); `409`/`422` are now attached iff the view routes through the seam, asserted in BOTH directions with a vacuity check. The three divergent routes are called out explicitly in `info.description`. |
| WARNING | `PATCH {"include_in_report": null}` wrote NULL to a NOT NULL column → 500 on Postgres for a plain bad request. (Found in self-review; confirmed by two reviewers.) | Strict `isinstance(bool)`, matching `scribble_update_group`. Boundary test over `null`/`"yes"`/`1`/`[]` plus an assertion the stored value is untouched. |
| WARNING | A truthy non-dict body (`[1,2]`, `123`, `true`) reached `set(data)` inside the view → 500. **Two sibling PATCH routes shared the bug.** | One `_json_object_or_400` helper next to `_idempotency_key` (which already defended itself the same way), applied at all three call sites. Test drives all three routes; `"hello"` is included because a string is iterable and answered 400 *by accident*, which is how the real 500 stayed invisible. |
| WARNING | The document claimed either id space addresses an engagement in a URL. True for 5 of 17 routes; 404 on the rest. | Reworded on the `Engagement` component to name the five that honour it. |
| WARNING | No `400` and no `409` documented. `409` (in-flight retry) became reachable on this blueprint **for the first time** with the idempotency fix; a client that does not know it retries harder. | Both added, plus `409` explained in `info.description`; `test_refusals_a_client_must_handle_are_documented`. |
| WARNING | `idempotency_keys` now holds client report prose, in a table with no tenancy key and no sweeper. | Not an exposure (per-principal slot, no reader, authorize-before-seam — verified). Routed as a **retention/lifecycle** gap to lotek#488 along with the root cause. |
| WARNING | The per-item tenancy test used one `client_id` for both engagements, so it proved row ownership, not tenancy. | `_engagement_via_pat` takes a `client_id`; the test now has BOTH arms — a same-client sibling (proves `_diagram_of`) and a different-client engagement (proves the engagement gate). |
| NOTE | The `POST` link route emitted no in-band audit row, so of "who added / who unpublished / who destroyed", only the first had no answer. | Added `link_attack_path`; all three verbs asserted in one test, because the gap was an asymmetry a per-verb test would have passed through. |
| NOTE | View docstrings — including dated internal security history — were published verbatim to any read-scoped token. | `_summary` now publishes the first paragraph only. |
| NOTE | `finding_id` means the finding's own id on `FindingSummary` and the PARENT finding on `Artifact`. | Cross-referenced in both component descriptions. |
| NOTE | The report route was declared `application/json` while streaming HTML/docx. | Real media types via `_RESPONSE_MEDIA_TYPES`, pinned by a test. |
| NOTE | The `order_index` re-pack wrote every sibling unconditionally — deadlock-shaped under concurrent deletes. | Only writes rows whose slot moved, with a `ponytail:` note naming the residual ceiling and the `FOR UPDATE` upgrade path. |
| NOTE | The conftest stub had no in-flight branch, so nothing exercised the host's `409` — the one place it was still kinder than production. | Added. |
| NOTE | The drift guard's public-document loop could never fail (the fallback schema is a non-empty dict). | Now rejects the fallback by its description, so the black-box half has teeth if the white-box half is ever refactored away. |
| NOTE | The `diagrams` deprecation deadline was written three different ways in three files, with nothing able to check any of them. | All three now point at **#121**, the tracking issue that removes the alias and names every site to touch. |
| — | INV-TENANCY-05: writes gate on `can_view_client` (client-coarse), not `can_operate_on`. Pre-existing and blueprint-wide. | Filed as **#123** rather than silently inherited. Changing it is a 16-route behaviour change needing a mounted test, not this branch's call. |

Two findings were declined, with reasons: the extension build id in `info.version` (core's
`/api/v1/openapi.json` already publishes an equivalent to the identical audience, so removing it here
buys nothing), and wrapping `promote-job`/`update_artifact` in the seam (a real behaviour change, out of
scope — the document now says plainly that they do not honour the key).

## Remaining

- [ ] Orchestrator runs the combined suite + `pytest -m invariant`, the merged-diff reviews, the
      `--ack-*` markers, and opens one PR. **This branch records no `--ack-tests` and opens no PR.**

## Notes / gotchas

- **`diagrams` → `attack_paths` is a breaking rename.** Both keys are emitted; the old one is
  documented as deprecated in `scribble/docs/SCRIBBLE.md` and in the PR body (this repo has no
  `CHANGELOG.md` — releases are auto-tagged, see `plans/chore-release-autonotes.md`). The removal is
  tracked by **#121**, and every site that mentions the deprecation points at that issue rather than
  restating a relative deadline — a review finding on this branch was that "one release" and "the
  release after next" had both been written, in three files, with nothing able to check either.
- **Deliberately NOT done:** a flat top-level `findings` key on `GET …/findings`. The issue offers
  "rename OR document the nesting"; duplicating every finding into a second list doubles the
  response for a large engagement, and the published OpenAPI document now describes the nesting
  precisely. `finding_id` IS added as a read alias (issue's fix #3).
- **Deliberately NOT done:** rejecting a second POST with the same `diagram_ref` (issue's fix #3,
  "would be a reasonable additional guard"). Re-linking a re-exported snapshot of the same vector
  diagram is legitimate; with `Idempotency-Key` honoured the retry case is already covered.
- **Two host-seam properties this fix makes REACHABLE for the first time, worth knowing** (both are
  pre-existing behaviour of lotek's `src/app/idempotency.py`, not introduced here — before this branch
  nothing on this blueprint was ever memoized, so neither could fire):
  1. **The request fingerprint hashes only the first 64 KiB of the body** (`_HASHED_BODY_BYTES`). A
     caller that reuses ONE `Idempotency-Key` for two DIFFERENT attack paths whose first 64 KiB happen to
     match (same vector export preamble, different graph further in) gets the first one replayed instead
     of the `422` the mismatch is supposed to earn — and the second diagram is silently not stored. It
     needs a caller to misuse the key, which is the thing the key exists to prevent, but the failure mode
     is silent. → **lotek-side issue worth filing**; nothing sound can be done extension-side (folding
     content into the slot key is explicitly the wrong design — see that module's docstring).
  2. **A response over 64 KiB is still not memoized** (`_MAX_STORED_RESPONSE_BYTES`); the claim is
     released and the retry re-executes. That is unchanged behaviour and it bounds the growth of
     `idempotency_keys.response_json` to ≤64 KiB per key, but it means a very large
     `PATCH /findings/<id>` response is still not replay-protected.
- Standalone scribble (no mounting host) still has no idempotency store — `_with_idempotency` fails
  open exactly as every other host seam in this package does. Production is always mounted.
