# Plan: feat/scribble-machine-findings-crud

- **Branch:** `feat/scribble-machine-findings-crud`  (worktree: `.claude/worktrees/ux-findings-crud`, off `main`)
- **PR:** not opened yet (the orchestrator opens it)
- **Status:** 🟢 ready to merge — closes ext#41 and ext#47. **Adversarial review round 1 (2026-08-17):
  1 BLOCK + 4 CONCERNs, all five fixed** (see "Review round 1" below); nothing refuted.
- **🔴 Session interrupted 2026-08-17.** The session doing round 1 was killed mid-flight by an API
  limit while extending the string-boundary sweep to the LAST writer on the blueprint, the evidence-upload
  route. Its work was sitting uncommitted in the worktree (`api_pat.py` + `test_machine_findings_crud.py`,
  no untracked files) and was landed by the next session as commit "bound and type-check the evidence
  upload's filename/caption". It was coherent but its cap was WRONG — see §24 — which the resumed
  session found by adding the accepted-side assertion its sibling test already had.

## Purpose

Two client-reported defects from the TeamsPlus deliverable punch list, both in scribble's PAT machine
API (`scribble/api_pat.py`):

- **ext#41 — the machine API can CREATE findings and nothing else.** 13 routes, and the only findings
  route was `POST /engagements/<id>/findings`. No read-back, no list, no edit/delete/move/group. So an
  agent authoring a report over a PAT could not fix a title, could not group or reorder, and could not
  even see what it had created (`GET /engagements/<id>` returns a bare `finding_count`). The cookie UI
  has all of it and the model already carries `group_id`/`order_index`/`parent_id`/
  `FindingGroup.order_index`/`order_mode` — a missing surface, not a data-model change.
- **ext#47 — `client not found` dead-ends the caller.** `POST /api/v1/clients` → 201, then
  `POST /scribble/machine/engagements` → 404 `client not found`, because a core client is record-only
  until `POST /api/v1/engagements` self-grants the creator an operator membership. The RULE is correct
  (`can_view_client_id` is membership-only on purpose — admitting the owner axis is a cross-tenant
  escalation). Only the message was wrong.

## Done

- [x] Plan file committed first.
- [x] **#47** (own commit, `b02acc0`) — `api_pat._client_not_found()`: one static next-step hint, appended
      unconditionally so the "no such client" and "exists but you hold no grant" refusals stay
      byte-identical. Verified against core `origin/main` (`d31e424`) before writing the message:
      `POST /api/v1/engagements` exists, is `_require_admin_or_404` (a non-admin gets 404, not 403), and
      self-grants the creator an `operator` membership — the hint says the admin-only part too, because a
      hint naming only the route would dead-end a non-admin one step later. 5 tests in
      `tests/test_machine_client_onboarding.py`.
- [x] **#41** — `scribble/findings_service.py`: the board mutation algorithms extracted out of
      `engagement_ui.py` (`display_order`, `ungrouped_display_order`, `reindex`, `place_finding`,
      `reorder_groups`, `create_group`, `delete_group`, `delete_finding`). Both surfaces now call it, so
      ordering/cascade behaviour cannot drift. The 56-test cookie board suite is the regression net for
      the extraction and stayed green throughout.
- [x] **#41** — 10 new `machine_bp` routes (13 → 23), 71 tests in `tests/test_machine_findings_crud.py`.
- [x] **#41** — `tests/test_scribble_machine_tenancy.py`: new `_CHILD_ID_ARGS` classification so the
      existing DENY/ALLOW sweeps cover every new route, with children seeded per request.
- [x] Docs: `docs/SCRIBBLE.md` — the machine-API table (was stale at "Nine routes"), a new "The board"
      section, the ordering semantics, three refusal-code rows, the drag-board note about the shared
      service, and the walkthrough's steps 4–5 (which previously implied a browser).
- [x] **#41 (class, not instance)** — the evidence-upload route `POST /engagements/<eid>/artifacts`,
      the last string writer on this blueprint, now type-checks `filename`/`caption` (a dict reached
      `mimetypes.guess_type` and the `caption` `Text` column) and bounds `filename` — at the
      FILESYSTEM's limit, not the column's. §24.
- [x] Red-then-green transcript for every new guard, captured verbatim below.

## The new surface (10 routes, 13 → 23)

| Method · path | Scope |
|---|---|
| `GET /engagements/<eid>/findings` | read |
| `GET /findings/<fid>` | read |
| `PATCH /findings/<fid>` | write |
| `DELETE /findings/<fid>` | write |
| `POST /findings/<fid>/move` | write |
| `POST /engagements/<eid>/findings/move` (bulk) | write |
| `POST /engagements/<eid>/groups` | write |
| `PATCH /engagements/<eid>/groups/<gid>` | write |
| `DELETE /engagements/<eid>/groups/<gid>` | write |
| `POST /engagements/<eid>/groups/reorder` | write |

## Notes / gotchas

- **Tenancy is resolved from the ENGAGEMENT, never the body.** `/findings/<fid>` carries no engagement in
  the URL, so `_visible_finding` loads the row, follows its stored `engagement_id` to the engagement, and
  asks the same `can_view_engagement` predicate every other route here asks. A caller cannot pair one of
  its own engagement ids with another tenant's finding id. Missing and not-visible are the SAME 404.
- **A foreign `group_id` on `move` is a 404, not a silent drop — a deliberate divergence from the issue
  text.** `add_finding`/`upload_artifact` silently drop a foreign `group_id`/`finding_id`, and the issue
  suggested matching that precedent. The code says otherwise for this operation: the cookie
  `move_finding` (`engagement_ui.py`) answers 404 for a missing or cross-engagement group, and silently
  dropping it on a move would move the finding OUT of its group — data loss the caller never asked for,
  reported as success. One identical message covers "no such group" and "another engagement's group", so
  no oracle. `PATCH` refuses `group_id` outright (pointing at `move`), so the ambiguity arises once, not
  twice.
- **The bulk move is atomic.** Any id in `finding_ids` not in the URL's engagement → 404 `finding not
  found` (the same body a nonexistent id gets) and NOTHING moves. Skipping unknown ids would make a
  foreign id a probe and leave the board half-arranged.
- **PATCH refuses unknown fields (400).** This module's house style is lenient parsing; the edit route
  breaks it on purpose. A typo'd field name is an agent's likeliest mistake and ignoring it returns 200
  for an edit that never happened.
- **A retried DELETE answers 404, and that is deliberate.** Authorization runs before the idempotency
  seam, so after a successful delete the retry never reaches the seam to replay the stored 200. Making it
  replay would mean consulting the seam before deciding whether the caller may touch the row, or letting
  the tenancy check pass on a row it can no longer resolve. The seam still earns its place on DELETE for
  the CONCURRENT case (its DB unique constraint stops a second delete of the same row).
  `test_a_retried_delete_404s_because_authorization_runs_BEFORE_the_seam` pins the ordering;
  `test_a_retried_patch_with_the_same_key_executes_once` pins the seam itself.
- **🔴 The one real bug this work introduced and the tests caught:** `place_finding` originally set
  `finding.group_id` (the FK column). SQLAlchemy only syncs a backref when the RELATIONSHIP is assigned,
  so `FindingGroup.findings` stayed stale — invisible for one move per session (all the cookie board ever
  does, and all 52 cookie board tests passed) and wrong for the bulk move, where every finding after the
  first computed its destination siblings from a collection that still looked empty and landed at index 0.
  Fixed by assigning `finding.group` *and* the column (the relationship alone only writes the FK at flush,
  and both move routes read `finding.group_id` to build their response). See transcript 6.
- **`content_html` is re-derived on PATCH** through the same `render_block` + `autosave_api._artifact_url`
  the cookie autosave uses, so the editor/preview cache cannot drift from `content_json`. The report
  renders from `content_json` (`reporting/context.py:165`), so this is cache hygiene, not report
  correctness.
- **String values are length-capped at the boundary** (`_COLUMN_MAX_LEN`, `_GROUP_NAME_MAX_LEN`) and
  `cvss_score` is bounded to 0.0–10.0. Found in self-review: these are `String(n)` columns, so on
  Postgres — prod — an over-long value raises `StringDataRightTruncation` and the caller gets a 500 for a
  bad request, while SQLite (this suite's backend) accepts it silently. Same shape as the uuid/Integer
  trap. The range check on `cvss_score` also refuses the `NaN`/`Infinity` tokens Python's JSON parser
  accepts by default. **The first version capped PATCH only** — the sibling CREATE route on the same
  blueprint wrote the same columns unbounded, so the guard the commit existed for was still reachable one
  route over — as did `POST /engagements` and `POST /templates`. Fixed in review round 1: every string this
  blueprint writes is now bounded, and the dict is named `_COLUMN_`, not `_PATCH_`, precisely so the next
  writer does not read it as one route's business.
- **`_sev_value` → `_enum_value`** (pure rename, 3 call sites): the finding serializers needed the same
  enum unwrap for `confidence`/`status`/`order_mode`/`kind`, and four more copies of a one-liner was the
  alternative.

## Review round 1 — adversarial review, 2026-08-17 (1 BLOCK + 4 CONCERNs, all fixed)

Nothing was refuted; every finding reproduced. The BLOCK was the one that mattered, and the reviewer's
diagnosis of how it survived 71 tests is correct: the delete test asserted the artifact cascade — the
cascade the author was thinking about — and nothing on the branch seeded a parent with children on a WRITE
path.

1. **BLOCK · `DELETE /findings/<id>` 500'd on a promoted PARENT.** `EngagementFinding.parent_id` is a
   self-FK with no `ondelete` and no ORM relationship, so nothing cleared it: the DELETE raised
   `IntegrityError` (SQLite, FKs on) / `ForeignKeyViolation` (prod Postgres), nothing was deleted and the
   audit row rolled back with it. `promote_job` builds parent+children by DEFAULT for every finding that
   resolves to a template, so the single operation ext#41 was filed about could not be performed on a
   promoted finding — the exact thing this branch exists to fix. Reproduced with the reviewer's script
   before touching anything (`/tmp/advrev/repro_parent_delete.py` → `FOREIGN KEY constraint failed`).
   **Fix:** `findings_service.detach_children` — children are DETACHED (`parent_id` → NULL), not deleted,
   and their ids come back in `detached_children` (response + audit row). Chosen over cascading the delete
   because the parent is a synthesized umbrella row over the template's write-up while each child holds the
   irreplaceable per-host evidence (own target, variables, `source_finding_id`, artifacts) — the same reason
   `delete_group` detaches instead of destroying. One DELETE must not take N unnamed findings with it, and
   the renderer already draws an unresolvable-parent child top-level (`tests/test_smoke.py` covers it).
   The cookie route shares the fix through the same service function.
   - **🔴 Same defect class found while fixing it, NOT reported by the reviewer: the cookie
     `engagement_delete` route could not delete an engagement holding any promoted aggregation at all.**
     `Engagement.findings` cascades `delete-orphan`, and with no ORM relationship on the self-FK SQLAlchemy
     has no dependency to order those DELETEs by — it emits them as one unordered batch and the child rows'
     FK fails. Pre-existing (not introduced here), a prod 500, and fixed with
     `findings_service.flatten_nesting` + a test (`tests/test_board.py`), since it is the same one-line
     omission on the same column and there was no cookie engagement-delete test at all.
2. **CONCERN · a negative `order_index` silently REVERSED a bulk move.** `place_finding` clamps with
   `max(0, min(requested, len))`, so every negative offset collapsed to slot 0 and each successive insert
   pushed the previous one down — 200, docstring promising the listed order was preserved, board in the
   opposite order. `order_index: -1` reversed a 2-item move. **Fix:** `_parse_move_target` refuses `< 0`
   with a 400 (0 already means "before the first", so a negative index cannot express anything a caller
   could have meant), on BOTH move routes.
3. **CONCERN · the bulk move's `moved[].order_index` was stale.** Read inside the placement loop, while
   each placement reindexes the whole destination — the response reported 0 for every entry of a 3-finding
   move that the database stored as 0,1,2. **Fix:** built from a pass over the placed findings AFTER the
   loop, plus a test asserting the reported map EQUALS the persisted one.
4. **CONCERN · `GET /engagements/<id>/findings` counted nested children as top-level findings** while its
   docstring claimed the listing "IS the document order". The listing is right and the docstring was wrong:
   the list is the BOARD list (children are their own rows there, and `order_index` on a move is a slot in
   exactly that flat list — nesting it would have made the move indices refer to a list the caller can no
   longer see). **Fix:** docstring + `docs/SCRIBBLE.md` corrected to say board list, and a new
   `top_level_count` answers "how many findings does the report show?" via
   `findings_service.rendered_top_level_count`. The nesting rule now lives ONCE
   (`findings_service.nested_child_ids`, which `reporting/context.py::_nest_findings` calls), and
   `test_top_level_count_matches_what_the_renderer_produces` asserts the count against
   `build_report_context`'s own output over an awkward board (nested cluster, excluded child, excluded
   parent, excluded group) rather than restating the rule.
5. **CONCERN · PATCH silently dropped an empty prose payload.** `{"title": "x", "description": ""}` → 200
   with the old prose intact; `{"description": ""}` alone → a misleading 400 "no updatable fields supplied".
   So there was no way to clear a block at all, and trying reported success — the very failure
   `_patch_content_blocks`' docstring says it exists to prevent, arriving by VALUE rather than by type
   (`_author_content_json`'s guards are truthiness tests, correct for a create, wrong for an edit).
   **Fix:** a supplied-but-empty `description`/`remediation`/`references` becomes an explicit empty
   ProseMirror doc — what the cookie editor's autosave already stores for a cleared block, and what the
   renderer treats as an absent section. Still routed through the sanitizer, so there is one path into
   `content_json`. `{"content_json": {}}` remains "no blocks supplied", documented.
6. **CONCERN · the width caps were PATCH-only** while the CREATE route on the same blueprint wrote the same
   `String(n)` columns unbounded (`POST …/findings {"title": "x"*600}` → 201; the identical PATCH → 400), and
   assigned `target_host`/`target_url` with no type check at all. The reviewer is also right that the plan
   presented this as a solved class. **Fix:** `_PATCH_MAX_LEN` → `_COLUMN_MAX_LEN`, hoisted next to
   `_opt_str` with `_too_long`, plus `_parse_target_fields` — ONE type-checked, length-capped, `str()`-coerced
   parse of the three `target_*` fields shared by all three create branches (the template and promote
   branches previously bound a raw int to a `String(16)` port column, which is a Postgres `DataError`).
   - **🔴 Also not reported: `POST /engagements` and `POST /templates` had the identical hole** — the
     reviewer's own probe 6 still answered `201` for a 5000-char engagement `name` (`String(255)`) after the
     findings-create fix, and `scope_type`/`company_name` were read raw from the body (a dict binds straight
     to a `String` column). Both routes now go through `_opt_str` + `_too_long`, so every string this
     blueprint writes is bounded at the boundary — the class, not the instance. Creating the engagement is
     the first call any agent makes, which is exactly the wrong place to leave it.

## Out of scope (deliberate decisions, not oversights)

- `POST /findings/<id>/artifacts/reorder` and `POST /findings/<id>/blocks/<block>` (two rows of #41's
  parity table). `PATCH /findings/<fid>` covers block content in one call, which is what an agent needs;
  artifact reordering has no reported client need and would add a third id axis to the tenancy sweep.
- #41's suggested drift test ("every `engagement_ui`/`artifacts_api` mutation has a machine counterpart").
  It would fail today on exactly the two routes above, i.e. it encodes a policy decision this branch is
  not making. The compensating control is the classification guard in
  `tests/test_scribble_machine_tenancy.py`, which does fail closed on a new machine route.
- **Residual, and deliberately left for the reviewer: a unicode `filename` can still overrun `NAME_MAX`.**
  The cap counts the caller's characters, but `save_bytes` stores `secure_filename(filename)`, and
  `secure_filename` NFKD-normalizes — which can EXPAND. Measured:
  `len(secure_filename("½" * 222)) == 444`, so that name passes the 222 cap and still raises
  `ENAMETOOLONG` → 500. Closing it means either importing `secure_filename` into `api_pat` (duplicating
  storage's naming rule in the API layer, where the constant already half-lives) or truncating `safe_name`
  inside `artifacts_storage.save_bytes` — which is the right place and would fix the cookie upload path
  too, but is a shared-module change this branch did not otherwise need. Not a regression: it is strictly
  narrower than the hole that existed before the cap.
- Anything mounted. Each extension's suite injects its own stub host, so the seam gap class
  (`g.principal`) is invisible here by construction — a mounted test in lotek's
  `tests/test_scribble_*` is still owed by whoever re-pins the extension, per CLAUDE.md.

## Red-then-green

Verbatim pytest output of each guard being broken, watched fail, and restored. Nothing is recorded here
that was not actually run. Full captures are reproducible with the same one-line edits described.

### 1. #47 · the client 404 must name the next step

`tests/test_machine_client_onboarding.py::test_client_not_found_names_the_next_step` — reverted
`_client_not_found`'s detail to the old bare string:

```
--- RED (hint removed) ---
        detail = body["detail"]
>       assert "record-only" in detail
E       AssertionError: assert 'record-only' in 'client not found'
tests/test_machine_client_onboarding.py:73: AssertionError
FAILED tests/test_machine_client_onboarding.py::test_client_not_found_names_the_next_step
1 failed, 4 passed in 1.99s

--- GREEN (hint restored) ---
5 passed in 2.41s
```

### 2. #47 · adding the hint must NOT create an existence oracle

`…::test_client_refusal_is_byte_identical_for_missing_and_ungranted` — broke it with the "helpful"
version of the same fix: look the client row up and say which of the two cases the caller hit.

```
--- RED (refusal reveals existence) ---
>       assert ungranted.data == missing.data, (
E       AssertionError: the two client refusals differ — that is an existence oracle over the client id
E       space: b'{"client_exists":true,"detail":"client not found, or you hold no membership under it. …
E       vs b'{"client_exists":false,"detail":"client not found, or you hold no membership under it. …
E       At index 17 diff: b't' != b'f'
FAILED tests/test_machine_client_onboarding.py::test_client_refusal_is_byte_identical_for_missing_and_ungranted
1 failed, 4 passed in 2.28s

--- GREEN (oracle removed) ---
5 passed in 1.86s
```

The fixture gives `UNGRANTED` a REAL `scribble_clients` row and `NONEXISTENT` none, so the two requests
genuinely differ in the world. Without that row the assertion would hold for a route that leaked the
difference — a test of nothing.

### 3. #41 · the route-classification guard (this one went red on its own)

Not an induced break: adding the 10 routes made the EXISTING fail-closed guard in
`tests/test_scribble_machine_tenancy.py` go red before it was updated, which is the guard working.

```
--- RED (new routes, not yet classified) ---
E               AssertionError: scribble_machine.scribble_delete_finding has an unrecognized view arg 'finding_id'
tests/test_scribble_machine_tenancy.py:97: AssertionError
FAILED tests/test_scribble_machine_tenancy.py::test_every_machine_route_is_classified - AssertionError: machine route(s) neither engagement-scoped, scoped-list, no...
FAILED tests/test_scribble_machine_tenancy.py::test_every_engagement_scoped_machine_route_denies_a_foreign_client
FAILED tests/test_scribble_machine_tenancy.py::test_every_engagement_scoped_machine_route_allows_a_granted_token
3 failed, 32 passed in 14.03s

--- GREEN (child-id classification + per-request seeded children) ---
16 passed in 7.85s
```

### 4. #41 · a finding in another tenant's engagement is not addressable

`…test_machine_findings_crud.py -k foreign_finding` — removed the `can_view_engagement` call from
`_visible_finding`:

```
--- RED (no engagement-level tenancy check) ---
E       AssertionError: {'analyst_notes': None, 'artifacts': [], 'category': None, 'children': [], ...}
E       assert 200 == 404
E       AssertionError: {'deleted': True, 'engagement_id': 1, 'finding_id': 1}
E       assert 200 == 404
E       AssertionError: {'engagement_id': 1, 'finding_id': 1, 'group_id': None, 'order_index': 0}
E       assert 200 == 404
FAILED …::test_every_findings_route_denies_a_foreign_finding[GET-{M}/findings/{fid}-None]
FAILED …::test_every_findings_route_denies_a_foreign_finding[PATCH-{M}/findings/{fid}-body1]
FAILED …::test_every_findings_route_denies_a_foreign_finding[DELETE-{M}/findings/{fid}-None]
FAILED …::test_every_findings_route_denies_a_foreign_finding[POST-{M}/findings/{fid}/move-body3]
FAILED …::test_a_foreign_finding_and_a_missing_one_are_byte_identical[GET-…]
FAILED …::test_a_foreign_finding_and_a_missing_one_are_byte_identical[PATCH-…]
FAILED …::test_a_foreign_finding_and_a_missing_one_are_byte_identical[DELETE-…]
FAILED …::test_a_foreign_finding_and_a_missing_one_are_byte_identical[POST-…/move-body3]
8 failed, 1 passed, 52 deselected in 6.21s

--- GREEN ---
9 passed, 52 deselected in 6.26s
```

Note what the RED shows: another tenant's finding was READ in full, EDITED, and DELETED. The byte-identity
half went red at the same time, which is the point of having both.

### 5. #41 · a group from another engagement is not attachable

Removed the `group.engagement_id != engagement_id` check from `_group_of`:

```
--- RED (no cross-engagement check on the group id) ---
E       assert 200 == 404          # move into another engagement's group succeeded
E       assert (200, b'{"eng..._index":0}\n') == (404, b'{"det...ot_found"}\n')
E       AssertionError: assert 200 == 404
E        +    where <WrapperTestResponse streamed [200 OK]> = patch('/scribble/machine/engagements/1/groups/1', json={'name': 'hijacked'})
FAILED …::test_move_into_a_foreign_engagements_group_is_404_and_moves_nothing
FAILED …::test_the_group_refusal_is_identical_for_foreign_and_nonexistent
FAILED …::test_group_routes_refuse_another_engagements_group
3 failed in 2.89s

--- GREEN ---
3 passed in 2.91s
```

### 6. #41 · the bulk move preserves the caller's order (the real bug)

Reverted `place_finding` to setting only the `group_id` FK — the version this branch shipped first, and
the reason the whole cookie board suite is not sufficient evidence for a bulk route:

```
--- RED (FK-only assignment; FindingGroup.findings stays stale) ---
E           assert [1, 2, 3] == [2, 3, 1]     # the caller asked for [b, c, a]
E           assert [0, 0] == [0, 1]           # both findings landed at order_index 0
FAILED …::test_bulk_move_preserves_the_listed_order
FAILED …::test_bulk_move_collapses_duplicate_ids
2 failed, 52 passed in 39.20s

--- GREEN (assign the relationship, and the column for immediate readback) ---
54 passed in 33.01s
```

`2 failed, 52 passed` is the finding here: the 52 passing tests are the entire cookie board suite, green
against the broken version. One move per request cannot see this bug.

### 7. #41 · the bulk move is atomic

Made the membership check skip unknown ids instead of refusing:

```
--- RED (skip foreign ids instead of refusing) ---
        resp = client.post(
            f"{M}/engagements/{eid}/findings/move",
            json={"finding_ids": [mine, theirs], "group_id": gid},
        )
>       assert resp.status_code == 404
E       assert 200 == 404
FAILED …::test_bulk_move_with_a_foreign_finding_id_moves_nothing
1 failed in 1.50s

--- GREEN ---
1 passed in 1.82s
```

### 8. #41 · every new write route really requires `write` scope

Declared `@host.require_scope("read")` on `PATCH /findings/<fid>`:

```
--- RED (an edit route declaring read scope) ---
>       assert reached == [], f"a read-only token reached write route(s): {reached}"
E       AssertionError: a read-only token reached write route(s): [('PATCH', '/scribble/machine/findings/1', 200)]
FAILED …::test_read_token_cannot_reach_any_new_write_route
1 failed, 1 passed in 2.94s

--- GREEN ---
2 passed in 2.13s
```

This is only provable against a REAL scope-checking gate; under the conftest's no-op `require_pat_scope`
the wrong scope (or a missing decorator) looks perfectly gated.

### 9. #41 · deleting a finding takes its evidence

Reduced `findings_service.delete_finding` to a bare `db.delete(finding)`:

```
--- RED (no artifact cascade — rows orphan, files leak) ---
>           assert db.get(Artifact, artifact_id) is None
E           AssertionError: assert <scribble.models.Artifact object at 0x…> is None
FAILED …test_machine_findings_crud.py::test_delete_finding_takes_its_evidence_rows_and_files
FAILED tests/test_board.py::test_delete_finding_removes_finding_and_its_artifacts
2 failed in 2.26s

--- GREEN ---
2 passed in 2.67s
```

Both surfaces went red from one edit — which is the extraction doing its job.

### 10. #41 · PATCH cannot become a laxer path into `content_json`

Merged the caller's raw blocks over the sanitized ones (i.e. bypassed the ProseMirror sanitizer):

```
--- RED (blocks persisted verbatim) ---
>       assert "javascript:" not in str(content["impact"])
E       assert 'javascript:' not in "{'content':...ype': 'doc'}"
E         'javascript:' is contained here:
E           {'href': 'javascript:alert(1)'}, 'type': 'link'}], 'text': 'click', 'type': 'text'}], …
FAILED …::test_patch_merges_content_blocks_and_sanitizes_them
1 failed in 2.10s

--- GREEN ---
1 passed in 1.43s
```

### 11. #41 · an over-long value is a 400 here, not a 500 from Postgres

Found in self-review, not by a failing test — and it is the class of defect this suite structurally cannot
see, because it runs on SQLite. Removed the `_too_long` enforcement:

```
--- RED (no column-width cap) ---
>       assert over.status_code == 400, over.get_json()
E       AssertionError: {'analyst_notes': None, 'artifacts': [], 'category': None, 'children': [], ...}
E       assert 200 == 400
FAILED …::test_patch_refuses_a_value_that_would_overflow_its_column[title-512]
FAILED …::test_patch_refuses_a_value_that_would_overflow_its_column[category-255]
FAILED …::test_patch_refuses_a_value_that_would_overflow_its_column[cvss_vector-255]
FAILED …::test_patch_refuses_a_value_that_would_overflow_its_column[target_host-255]
FAILED …::test_patch_refuses_a_value_that_would_overflow_its_column[target_port-16]
FAILED …::test_patch_refuses_a_value_that_would_overflow_its_column[target_url-1024]
6 failed, 1 passed in 4.00s

--- GREEN ---
7 passed in 4.70s
```

Be precise about what this proves and what it does not: the RED above is SQLite accepting the over-long
value (200), so the test pins the boundary CHECK. It does not exercise the Postgres truncation error it
exists to prevent — the two `SCRIBBLE_TEST_PG_URL` tests are the only Postgres-backed ones in this suite
and they skip here.

### 12. #41 · `cvss_score` is bounded to the CVSS range (which also refuses NaN/Infinity)

```
--- RED (no range check) ---
>       assert resp.status_code == 400, (body, resp.get_json())
E       AssertionError: ({'cvss_score': inf}, {'analyst_notes': None, 'artifacts': [], …})
E       assert 200 == 400
FAILED …::test_patch_rejects_malformed_input[body3]   # 11
FAILED …::test_patch_rejects_malformed_input[body4]   # -1
FAILED …::test_patch_rejects_malformed_input[body5]   # Infinity
3 failed, 10 passed in 5.55s

--- GREEN ---
13 passed in 5.28s
```

Python's JSON parser accepts the `Infinity`/`NaN` tokens by default; both are floats, both reach the
column, and both render into a client's deliverable as a severity number that means nothing.

### 13. #41 · every mutating route emits an audit row

Removed the `_audit` call from `POST /findings/<id>/move`:

```
--- RED (the move lands with no audit row) ---
E       AssertionError: assert ['ext:scribbl...e_group', ...] == ['ext:scribbl..._groups', ...]
E         At index 3 diff: 'ext:scribble:move_findings' != 'ext:scribble:move_finding'
E         Right contains one more item: 'ext:scribble:delete_finding'
FAILED …::test_every_mutating_route_emits_an_audit_row
1 failed in 1.80s

--- GREEN ---
1 passed in 1.44s
```

## Red-then-green — review round 1

Same protocol as above: each fix's production code broken on purpose, the test watched fail, restored,
watched pass. Verbatim, nothing reconstructed.

### 14. BLOCK · deleting a parent must detach its children, not violate the self-FK

Dropped the `detach_children` call from `findings_service.delete_finding` (i.e. the state the branch was
reviewed in). ONE edit, both surfaces red — the extraction doing its job again:

```
--- RED (no child detach) ---
sqlalchemy.exc.IntegrityError: (sqlite3.IntegrityError) FOREIGN KEY constraint failed
[SQL: DELETE FROM scribble_findings WHERE scribble_findings.id = ?]
E   assert 500 == 200
E   assert 500 == 302
FAILED tests/test_machine_findings_crud.py::test_delete_a_parent_detaches_its_children_instead_of_violating_the_self_FK
FAILED tests/test_machine_findings_crud.py::test_deleting_a_parent_is_recorded_with_the_children_it_detached
FAILED tests/test_board.py::test_delete_finding_detaches_its_nested_children - assert 500 == 302

--- GREEN ---
3 passed, 147 deselected in 3.85s
```

The reviewer's own end-to-end probe (`/tmp/advrev/probe3.py` — promote a scan job over the machine API,
then DELETE the aggregated parent) run against the fix, as the acceptance check:

```
promote: 200 {'engagement_id': 1, 'parents': 1, 'promoted': 2, 'skipped': 0}
listed top-level ungrouped: [(1, 'Kerberoasting', None), (2, 'Kerberoasting', 1), (3, 'Kerberoasting', 1)]
count field: 3 top_level_count: 1 -- parents: [1] children: [2, 3]

finding 1 has 2 children -> DELETE it:
  DELETE status: 200
  body: {'deleted': True, 'detached_children': [2, 3], 'engagement_id': 1, 'finding_id': 1}

AFTER the parent delete:
  rows: [(2, 'Kerberoasting', None), (3, 'Kerberoasting', None)]
  count: 2 top_level_count: 2
```

### 15. the same defect on `engagement_delete` (found here, not reported)

Removed the `findings_service.flatten_nesting(db, engagement)` call:

```
--- RED (delete-orphan cascade hits the self-FK) ---
sqlite3.IntegrityError: FOREIGN KEY constraint failed
tests/test_board.py:907: assert 500 == 302
FAILED tests/test_board.py::test_engagement_delete_survives_a_promoted_parent_child_cluster

--- GREEN ---
1 passed, 53 deselected in 2.06s
```

### 16. a negative `order_index` is refused, not clamped

Removed the `order_index < 0` guard from `_parse_move_target`:

```
--- RED (clamped, as reviewed) ---
    assert 200 == 400   (×4)
FAILED …::test_move_refuses_a_negative_order_index[False--1]
FAILED …::test_move_refuses_a_negative_order_index[False--5]
FAILED …::test_move_refuses_a_negative_order_index[True--1]
FAILED …::test_move_refuses_a_negative_order_index[True--5]
4 failed, 92 deselected in 4.08s

--- GREEN ---
4 passed, 92 deselected in 4.46s
```

And the reviewer's damage claim reproduced independently while the guard was out
(`/tmp/advrev/neg_probe.py`, bulk move of A,B,C at `order_index: -5`):

```
--- guard removed ---
asked for order: ['A', 'B', 'C'] -> status 200
board now: ['C', 'B', 'A']
--- guard restored ---
asked for order: ['A', 'B', 'C'] -> status 400
board now: []
```

### 17. the bulk move reports the `order_index` it PERSISTED

Reverted `moved` to being appended inside the placement loop:

```
--- RED (mid-loop read) ---
E   AssertionError: reported {2: 0, 3: 1}, database holds {1: 0, 2: 2, 3: 1}
E   assert {2: 0, 3: 1} == {2: 2, 3: 1}
FAILED …::test_bulk_move_reports_the_order_index_it_actually_persisted

--- GREEN ---
9 passed, 87 deselected in 7.54s
```

Worth recording HOW the test had to be built, because the first version of it passed against the broken
code: with a non-negative index into a `FindingGroup` the mid-loop read happens to be correct (every move
flips the group to `manual`, so later inserts land after earlier ones). The observable case is the
**ungrouped bucket**, which has no `order_mode` and re-sorts by severity on every insert — a `low` written
at slot 0 ends up at slot 2 once a `medium` lands behind a `critical`. The negative-index case the reviewer
measured is now a 400, so it could not have carried this guard.

### 18. `top_level_count` is the RENDERER's number

Broke `rendered_top_level_count` back to `len(engagement.findings)`:

```
--- RED (board rows reported as report findings) ---
    assert body["top_level_count"] == 1
E   assert 3 == 1
    assert reported == rendered, "the listing's top_level_count disagrees with the renderer"
E   assert 7 == 3
FAILED …::test_list_findings_is_the_board_list_and_says_what_the_report_renders
FAILED …::test_top_level_count_matches_what_the_renderer_produces

--- GREEN ---
2 passed, 94 deselected in 2.44s
```

The second assertion is against `build_report_context`'s own output (7 board rows vs the 3 cards the report
draws), not a restatement of the nesting rule.

### 19. the nesting rule nests exactly ONE level (a gap this refactor exposed)

Moving the rule into `findings_service.nested_child_ids` meant asking what pinned it. Half of it was
unpinned: dropping the `parent.parent_id is None` condition passed the whole existing nesting suite.

```
--- BREAK (condition dropped), BEFORE the new test ---
8 passed, 113 deselected in 7.33s      # nothing noticed

--- RED (with tests/test_smoke.py::test_report_context_nests_exactly_one_level_deep) ---
E   AssertionError: assert ['10.0.0.1'] == ['10.0.0.1', '10.0.0.3']
FAILED tests/test_smoke.py::test_report_context_nests_exactly_one_level_deep

--- GREEN (condition restored) ---
14 passed in 11.15s
```

A grandchild (`parent_id` -> a finding that is itself a child) must render top-level; with the condition
dropped it vanished inside the child's card. The pre-existing orphan test covers "parent not in the list",
never "parent is not a parent".

### 20. an empty prose value CLEARS the block

Reverted `_patch_content_blocks` to letting `_author_content_json`'s truthiness guards swallow it:

```
--- RED (silently dropped) ---
E   AssertionError: assert {'content': [...'type': 'doc'} == {'type': 'doc', 'content': []}
    assert 400 == 200
FAILED …::test_patch_clears_a_prose_block_when_the_value_is_empty[description-payload0]
FAILED …::test_patch_clears_a_prose_block_when_the_value_is_empty[description-payload1]
FAILED …::test_patch_clears_a_prose_block_when_the_value_is_empty[remediation-payload2]
FAILED …::test_patch_clears_a_prose_block_when_the_value_is_empty[references-payload3]
FAILED …::test_patch_clears_a_prose_block_when_the_value_is_empty[references-payload4]
FAILED …::test_patch_clearing_a_block_alone_is_not_a_no_op_400 - {'detail': 'no updatable fields supplied'}
6 failed, 1 passed, 89 deselected in 6.80s

--- GREEN ---
38 passed, 58 deselected in 27.66s
```

Note the two shapes of the same bug in that RED: with another field alongside, a **200** for prose that was
never written; alone, a **400** claiming there was nothing to update.

### 21. the CREATE route bounds the same columns PATCH does

Reverted `_parse_target_fields` to the pre-fix raw pass-through and removed the direct-author
`title`/`cvss_vector` caps:

```
--- RED (create unbounded + untyped, as reviewed) ---
FAILED …::test_create_refuses_a_value_that_would_overflow_its_column[title-512]
FAILED …::test_create_refuses_a_value_that_would_overflow_its_column[cvss_vector-255]
FAILED …::test_create_refuses_a_value_that_would_overflow_its_column[target_host-255]
FAILED …::test_create_refuses_a_value_that_would_overflow_its_column[target_port-16]
FAILED …::test_create_refuses_a_value_that_would_overflow_its_column[target_url-1024]
FAILED …::test_create_refuses_a_wrong_typed_target_field[body0]   # {"target_host": {...}}
FAILED …::test_create_refuses_a_wrong_typed_target_field[body1]   # {"target_url": [...]}
FAILED …::test_create_refuses_a_wrong_typed_target_field[body2]   # {"target_port": {...}}
FAILED …::test_create_refuses_a_wrong_typed_target_field[body3]   # {"target_port": true}
9 failed, 1 passed, 86 deselected in 9.09s

--- GREEN ---
12 passed, 84 deselected in 10.82s
```

### 22. …and coerces an integer port to text — asserted at the BOUNDARY, because SQLite hides it

The first version of this test asserted the STORED value end-to-end and **passed against the broken code**
— so it went in the bin rather than in the suite. SQLite applies column affinity to a `VARCHAR`, so a raw
int is converted on write and read back as text either way:

```
$ python /tmp/advrev/port_probe.py        # int bound to String(16) on SQLite
ORM read: '8443'
raw typeof: [('text', '8443')]
```

The check therefore lives on `_parse_target_fields`, where it can actually fail:

```
--- RED (str() coercion removed) ---
E   TypeError: object of type 'int' has no len()
FAILED …::test_create_coerces_an_integer_target_port_to_text_at_the_boundary
1 failed, 11 passed, 84 deselected in 10.21s

--- GREEN ---
12 passed, 84 deselected in 9.93s
```

Be precise about what this proves: that the boundary hands a `str` to the column. The Postgres `DataError`
it exists to prevent is still not exercised anywhere in this suite — same honest limit as transcript 11.

### 23. …and so do the OTHER two create routes on this blueprint (found here, not reported)

The reviewer scoped the cap asymmetry to `POST …/findings`. Their own probe script answers the wider
question, and it was still red after that fix:

```
=== PROBE 6: create engagement name / group name unbounded on create? ===
POST /engagements name 5000 -> 201 {'id': 2, 'name': 'NNNNNNNN…
```

`Engagement.name` is `String(255)`, and `scope_type` (`String(64)`) / `company_name` (`String(255)`) were
read raw from the body — no width, no type check, so a dict bound straight to a `String` column.
`POST /templates` had the same for `name` (`String(512)`), `category` and `cvss_vector`. Creating the
engagement is the FIRST call any agent makes, so leaving that one would have been fixing the instance and
not the class. Reverted all of it to the pre-fix reads:

```
--- RED (both other create routes unbounded) ---
FAILED …::test_every_create_route_on_this_blueprint_bounds_its_strings[/engagements-body0-name-255]
FAILED …::test_every_create_route_on_this_blueprint_bounds_its_strings[/engagements-body1-scope_type-64]
FAILED …::test_every_create_route_on_this_blueprint_bounds_its_strings[/engagements-body2-company_name-255]
FAILED …::test_every_create_route_on_this_blueprint_bounds_its_strings[/templates-body3-name-512]
FAILED …::test_every_create_route_on_this_blueprint_bounds_its_strings[/templates-body4-category-255]
FAILED …::test_every_create_route_on_this_blueprint_bounds_its_strings[/templates-body5-cvss_vector-255]
6 failed, 96 deselected in 7.54s

--- GREEN ---
6 passed, 96 deselected in 7.38s
```

`_too_long` grew a `cap=` override for this: `name` is `String(128)` on a group, `String(255)` on an
engagement and `String(512)` on a template, so one lookup keyed on the JSON field name would have applied
one table's width to another's column. The two group-name checks now go through the same helper (identical
message text) instead of open-coding the comparison.

### 24. …and the evidence-upload route, the last string writer (found here, not reported)

`POST /engagements/<eid>/artifacts` read `filename` and `caption` raw out of the JSON body, so a dict
reached `mimetypes.guess_type` and the `caption` `Text` column — and no width check at all. Reverting the
guard:

```
--- RED (filename/caption unbounded and untyped) ---
FAILED …::test_artifact_upload_bounds_and_types_its_strings[body0-too long]  - assert 500 == 400
FAILED …::test_artifact_upload_bounds_and_types_its_strings[body1-string]    - assert 500 == 400
FAILED …::test_artifact_upload_bounds_and_types_its_strings[body2-string]    - assert 500 == 400
3 failed, 102 deselected in 3.11s
```

All three are 500s, not 400s — `resp.get_json()` is `None` in every one, which is the HTML error page.

**The cap is the FILESYSTEM's number, not the column's, and that is the part the interrupted session got
wrong.** It set 512 (`Artifact.filename` is `String(512)`) which reads right and refuses almost nothing:
`save_bytes` stores the bytes as `<uuid4hex>_<secure_filename>`, so the basename is 33 characters longer
than what the caller sent and passes `NAME_MAX` (255) at **223**. Measured directly, not reasoned about:

```
PROBE n=216 -> 201    PROBE n=220 -> 201
PROBE n=217 -> 201    PROBE n=222 -> 201
PROBE n=218 -> 201    PROBE n=223 -> 500      # 32 + 1 + 223 == 256 > NAME_MAX
PROBE n=219 -> 201
```

So a 300-character filename — well under 512 — still answered 500 WITH the guard in place. Cap is now
`255 - 32 - 1 == 222`. Red-then-green on the cap value itself, by putting 512 back:

```
--- RED (cap = the column width, 512) ---
FAILED …::test_artifact_upload_bounds_and_types_its_strings[body0-too long]  - assert 500 == 400
1 failed, 3 passed, 102 deselected in 3.45s

--- GREEN (cap = 222) ---
4 passed, 102 deselected in 3.25s
```

**What let the wrong cap through is the missing half of the guard.** The parametrized test asserted only
the REFUSED side, so it passed for any cap at or above 223 — including one that refuses nothing anybody
was hitting. `test_every_create_route_on_this_blueprint_bounds_its_strings` (§23) already asserted
`at_cap -> 201` alongside `over -> 400`; this route's test did not, and that is exactly the assertion that
pins the number. Added as `test_artifact_upload_accepts_a_filename_AT_the_cap`. A one-sided boundary test
is a boundary test in name only.

<!-- Lifecycle: create + commit this FIRST thing when cutting the branch; keep it current; KEEP it —
     it merges to main with the branch as a durable record (since 2026-07-28; see CLAUDE.md
     "Per-branch plan"). -->
