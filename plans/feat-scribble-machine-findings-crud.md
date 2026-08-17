# Plan: feat/scribble-machine-findings-crud

- **Branch:** `feat/scribble-machine-findings-crud`  (worktree: `.claude/worktrees/ux-findings-crud`, off `main`)
- **PR:** not opened yet (the orchestrator opens it)
- **Status:** 🟢 ready to merge — closes ext#41 and ext#47

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
- [x] **#41** — 10 new `machine_bp` routes (13 → 23), 61 tests in `tests/test_machine_findings_crud.py`.
- [x] **#41** — `tests/test_scribble_machine_tenancy.py`: new `_CHILD_ID_ARGS` classification so the
      existing DENY/ALLOW sweeps cover every new route, with children seeded per request.
- [x] Docs: `docs/SCRIBBLE.md` — the machine-API table (was stale at "Nine routes"), a new "The board"
      section, the ordering semantics, three refusal-code rows, the drag-board note about the shared
      service, and the walkthrough's steps 4–5 (which previously implied a browser).
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
- **`_sev_value` → `_enum_value`** (pure rename, 3 call sites): the finding serializers needed the same
  enum unwrap for `confidence`/`status`/`order_mode`/`kind`, and four more copies of a one-liner was the
  alternative.

## Out of scope (deliberate decisions, not oversights)

- `POST /findings/<id>/artifacts/reorder` and `POST /findings/<id>/blocks/<block>` (two rows of #41's
  parity table). `PATCH /findings/<fid>` covers block content in one call, which is what an agent needs;
  artifact reordering has no reported client need and would add a third id axis to the tenancy sweep.
- #41's suggested drift test ("every `engagement_ui`/`artifacts_api` mutation has a machine counterpart").
  It would fail today on exactly the two routes above, i.e. it encodes a policy decision this branch is
  not making. The compensating control is the classification guard in
  `tests/test_scribble_machine_tenancy.py`, which does fail closed on a new machine route.
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

### 11. #41 · every mutating route emits an audit row

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

<!-- Lifecycle: create + commit this FIRST thing when cutting the branch; keep it current; KEEP it —
     it merges to main with the branch as a durable record (since 2026-07-28; see CLAUDE.md
     "Per-branch plan"). -->
