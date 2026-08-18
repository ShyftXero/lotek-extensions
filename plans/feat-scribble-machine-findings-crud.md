# Plan: feat/scribble-machine-findings-crud

- **Branch:** `feat/scribble-machine-findings-crud`  (worktree: `.claude/worktrees/ux-findings-crud`, off `main`)
- **PR:** not opened yet (the orchestrator opens it)
- **Status:** 🟢 ready to merge — closes ext#41 and ext#47. **Adversarial review round 1 (2026-08-17):
  1 BLOCK + 4 CONCERNs, all five fixed** (see "Review round 1" below); nothing refuted.
  **Adversarial review round 2 (2026-08-17): 2 BLOCKs + 3 CONCERNs. All five reproduced and fixed**
  (see "Review round 2"); nothing refuted, and one detail of a BLOCK's claim corrected — the sixth FK
  referrer (`scribble_finding_tags`) was already safe via the ORM's `secondary` cascade, which the branch
  now ASSERTS rather than assumes. Both BLOCKs were the same shape: a guard checked against one member of a
  set instead of against the set (the FK columns pointing at a finding; the values inside `content_json`) —
  so the pattern was then applied where nobody had reported it, which turned up (and fixed) a latent
  engagement-delete 500 in `scribble_report_renders`.
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
- [x] **#41 (round 2)** — `findings_service` now enumerates ALL SIX columns referencing
      `scribble_findings.id` with a declared disposition each (`clear_finding_referrers`,
      `prepare_engagement_delete`), so `DELETE /findings/<id>` and the cookie engagement delete stop 500ing
      on a finding anyone has opened in the editor / linked to a checklist / promoted with variables. A
      metadata-derived guard test fails when a seventh referrer appears unclassified.
- [x] **#41 (round 2)** — `content_json`'s VALUES are type-checked (`schema.is_doc`) on PATCH *and* on both
      create routes, so a non-doc block is a 400 instead of a 200/201 over silently emptied prose.
- [x] **#41 (round 2)** — id lists bounded (`_BULK_ID_LIST_MAX = 500`) and the bulk move's pre-check
      collapsed from one query per id to one query.
- [x] **#41 (round 2)** — NUL bytes escaped at the boundary (`_nul_safe`, mirroring core's
      `app/text_safety.py`), closing the Postgres-only 500 SQLite cannot show.
- [x] **#41 (round 2)** — the unicode-`filename` residual CLOSED in `artifacts_storage.save_bytes` (the
      layer that knows the final name), which fixes the cookie upload path too; `docs/SCRIBBLE.md`
      corrected — it had asserted the 500 was handled.
- [x] **#41 (round 2, unreported)** — the same enumeration applied to `scribble_engagements.id`:
      `ReportRender` rows are cleared in `prepare_engagement_delete` (a latent 500) and a second guard test
      requires every engagement referrer to be cascade-covered or declared.
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

## Verified (round 2, 2026-08-17)

```
cd scribble && uv run --extra dev pytest -W ignore      741 passed, 2 skipped in 320.42s   RC=0
uvx ruff check <6 changed files>                        All checks passed!
uv run --extra dev pyrefly check <6 changed files>      0 errors
```

Round 2 adds 18 collected tests across the two modules — `pytest --co -q` now reports
`tests/test_machine_findings_crud.py: 121` (was 106, the count the reviewer measured) and
`tests/test_board.py: 57`. Every new guard has its red-then-green transcript below (§25–§34), including one
whose FIRST version was vacuous and stayed green when the fix was reverted (§30) — that one is recorded as
prominently as the guards themselves, because it is the same "checked only on the side where it cannot fail"
error the reviewer named.

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

## Review round 2 — adversarial review, 2026-08-17 (2 BLOCKs + 3 CONCERNs, all fixed)

Nothing refuted. Every finding reproduced first, in this worktree's own suite, before any code moved — the
five repro cases are in the transcripts below (25–29). The reviewer's own summary of the shape is the useful
part and is recorded because it generalizes: **both BLOCKs were a fix applied to ONE instance of a class
while its siblings were left, in code this branch deliberately edited, behind a docstring claiming the class
was closed.** Round 1's own BLOCK was the first instance of that same pattern (`parent_id`), which is how
the second one hid: fixing it and writing the test made the area look settled.

1. **BLOCK · `DELETE /findings/<id>` still 500'd for a whole class of findings.** `grep 'scribble_findings.id'
   models.py` returns **six** columns; `delete_finding` handled two (`parent_id` via `detach_children`,
   `Artifact.finding_id` explicitly) and its docstring asserted the cascade was complete ("a finding IS its
   content"). Reproduced at 500 for three of the remaining four: a `CollabDoc` (which the live co-editing
   room's `_persist_room` writes the moment a human opens that block in the browser — so ANY finding an
   author has touched in the editor), an `EngagementChecklistItem.finding_id` (set through the supported
   `POST /scribble/api/engagement-checklist-items/<iid>` route), and a finding-scoped `VariableValue` (which
   promotion writes per host). `sqlite3.IntegrityError` here, `ForeignKeyViolation` on prod Postgres,
   transaction rolled back, finding not deleted, audit row lost. The same gap made the cookie
   `POST /engagements/<id>/delete` a 500 as well — reproduced — *despite* round 1 adding
   `test_engagement_delete_survives_a_promoted_parent_child_cluster`, which certifies that path for
   `parent_id` only. So the reported symptom ("an agent's only recovery is delete-and-recreate, which it also
   can't do") survived round 1 for any finding a human had opened or a checklist had linked.
   - **Fix:** the FK set is now ENUMERATED at the top of `findings_service.py` with a disposition per member —
     owned state DELETEs (`CollabDoc`, `VariableValue`), a cross-link NULLs (`EngagementChecklistItem`,
     the `delete_group`/`detach_children` rule: a checklist must not lose an item because the finding
     documenting it was deleted), and the three handled elsewhere are named with their handler.
     `clear_finding_referrers` does the work in one Core statement per referring table; `delete_finding` calls
     it, and the engagement path gets `prepare_engagement_delete` — ONE named entry point instead of two calls
     a future engagement-delete route has to remember, since "remembered one of them" is exactly how this
     shipped.
   - **Guard:** `test_every_column_referencing_a_finding_has_a_declared_delete_disposition` derives the FK set
     from `Base.metadata` and fails when it differs from what the module declares. A *seventh* referrer is
     therefore a red test, not a prod 500. Derived from the metadata deliberately — a hand-written table list
     would be the same defect it exists to catch.
   - **🔴 One claim corrected.** The reviewer listed `scribble_finding_tags.finding_id` among the unhandled
     columns but (correctly) did not claim to have reproduced it. It was already safe: `EngagementFinding.tags`
     is a `secondary` relationship and SQLAlchemy deletes the association rows with the parent. Verified, not
     assumed — `test_delete_finding_clears_every_row_that_references_it` seeds a `FindingTag` and asserts the
     row is gone, so "the ORM handles it" is now a tested claim rather than a comment.
2. **BLOCK · `PATCH /findings/<id>` destroyed authored prose and answered 200.** `_patch_content_blocks`
   validated that `content_json` was a dict and never its VALUES, and `sanitize_prosemirror` replaces any
   non-`doc` root with `schema.empty_doc()` (deliberately — an untrusted caller must not smuggle a non-doc
   root past the walker). So `{"content_json": {"description": "Updated description text"}}` → **200**, the
   stored block became `{"type":"doc","content":[]}`, and the response echoed the emptied doc as if that were
   the edit. Reproduced side by side with the cookie twin, which 400s on the identical body
   (`autosave_api.autosave_block` gates on `schema.is_doc`) — so the branch's "the two surfaces must not
   diverge" principle was violated precisely where it mattered most. `test_patch_rejects_malformed_input`
   covered `{"content_json": "not-an-object"}` and nothing inside the object: the container's type was
   guarded, its elements' were not.
   - **Fix:** `_non_doc_blocks_error` reuses the exact `schema.is_doc` predicate the cookie route uses, and is
     applied on PATCH **and on both create routes** (`POST …/findings`, `POST /templates`). Create was not in
     the finding, but a 201 for prose that was never stored is the same silent success as a 200, and a check
     only one of two writers consults is not a boundary — the lesson `_COLUMN_MAX_LEN` is named for, applied
     without waiting for a third review round to report it.
   - Both sides asserted: five parametrized refusal cases (string, node-rooted dict, number, list, null), a
     test that the stored prose is byte-identical after the refusal, and
     `test_patch_still_accepts_a_real_prosemirror_doc_for_a_NON_default_block` — because a refusal that is too
     broad would "close" the loss by closing the feature (`content_json` is the only way to author a
     non-default block such as `impact`).
3. **CONCERN · the bulk move amplified one request into N database round trips.** `finding_ids` was unbounded
   and the pre-check did one `db.get` per id before it could refuse: the reviewer measured **12.73 s** for
   20,000 nonexistent ids on SQLite, and with core's 256 MiB `MAX_CONTENT_LENGTH` a ~7 MB body carries ~1M
   ids. Correct, and the framing is the sting: this branch built `_COLUMN_MAX_LEN` for "unbounded input at the
   boundary" and left uncapped the one list whose LENGTH costs work per element.
   - **Fix:** `_BULK_ID_LIST_MAX = 500`, checked before the list is walked, on `finding_ids` and on
     `reorder_groups`' `order`; and the pre-check is now a single
     `SELECT id … WHERE id IN (…) AND engagement_id = …`, with the same refusal and the same atomicity. The cap
     stays even with the query gone because `place_finding` re-derives the destination's display order per
     placement, so a bulk move is O(N²) CPU regardless of query count. The test asserts BOTH halves — the 400,
     and (via a `before_cursor_execute` listener) that 50 ids cost exactly one SELECT, so the pre-check cannot
     silently regress to N.
4. **CONCERN · NUL bytes were never handled, only lengths.** Correct, and the reviewer could not prove it here
   (no Postgres on the box) — but core's own code documents the class: `app/text_safety.py::nul_safe` exists
   because ONE `job_events.line` row with 494,793 NULs killed a real migration. Postgres refuses a NUL in a
   bind; SQLite stores it, so this suite is blind to it — the identical blindness the length caps were added
   for. `analyst_notes` is the widest door (`Text`, no length cap at all) and scan output is exactly where a
   stray NUL comes from.
   - **Fix:** `_nul_safe` in `api_pat.py`, applied in the two funnels every accepted string passes through
     (`_opt_str`, and `_parse_finding_patch`'s own parsing), BEFORE the length caps as core's docstring
     instructs. Escaped to `␀` with a count marker rather than deleted (the evidence must not silently differ
     from what the tool emitted) or refused (an agent should not be blocked by a byte it does not control) —
     the same choice, symbol and marker as core, duplicated rather than imported because scribble must boot
     standalone.
   - **Residual, stated rather than implied:** this covers the strings THIS blueprint accepts. `promote_job`
     copies a host `FindingDTO`'s title/target text into the same columns without passing through either
     funnel; on a mounted host that text has already been through core's sanitizer, and closing it properly
     wants a `TypeDecorator` on the model columns (every writer, cookie and machine and promote, in one
     place) — a schema-wide change this repair round is not the moment for.
5. **CONCERN · the artifact `filename` residual, shipped behind a doc claiming it was closed.** The branch
   disclosed it in this plan's Out-of-scope, but `docs/SCRIBBLE.md` simultaneously told readers a longer name
   "is `ENAMETOOLONG` on write rather than a truncated column" — documentation asserting the 500 was handled.
   Reproduced: `"½" * 200 + ".png"` is 204 characters, passes the 222 cap, and `secure_filename` NFKD-expands
   it to 404 → `OSError: [Errno 36]` → **500**, from the cookie upload path too.
   - **Fix:** taken at the layer the reviewer recommended — `artifacts_storage._bounded_name`, applied inside
     `save_bytes` after `secure_filename`, preserving the extension. The API's 222 cap stays as the fast 400.
     The `docs/SCRIBBLE.md` sentence is corrected, and the test asserts the stored basename against the real
     `NAME_MAX` (bytes) and that the `.png` survived, so a cap that merely *differs* from the filesystem's
     limit would not pass.

6. **Not reported by either round, found by applying the pattern the reviewer named.** "Enumerate the set,
   then guard it" — asked about `scribble_engagements.id` instead of `scribble_findings.id`, it turns up a
   sixth referrer that no relationship cascades: `scribble_report_renders.engagement_id`. One row makes
   `POST /engagements/<id>/delete` a 500 (reproduced, transcript 33). It is LATENT — nothing instantiates
   `ReportRender` yet (schema-frozen for a later phase) — which is exactly how it would have shipped: the
   first code to write one breaks engagement delete somewhere far from itself. Cleared in
   `prepare_engagement_delete`, with the future writer's file-unlink obligation recorded next to the
   constant, plus the engagement-side twin of the FK guard (transcript 34). Stopping at the table that had
   already bitten us would have been the same mistake one table over.

**What round 2 did NOT change.** The reviewer's "what survived my attack" list (tenancy anchored on the
row's stored `engagement_id`, byte-identical refusals, authorization before body parsing, real scope
enforcement, the audit/idempotency seams, the `findings_service` extraction being behaviour-identical) was
checked and left alone. The two out-of-scope parity routes stay out of scope for round 1's reasons.

## Out of scope (deliberate decisions, not oversights)

- `POST /findings/<id>/artifacts/reorder` and `POST /findings/<id>/blocks/<block>` (two rows of #41's
  parity table). `PATCH /findings/<fid>` covers block content in one call, which is what an agent needs;
  artifact reordering has no reported client need and would add a third id axis to the tenancy sweep.
- #41's suggested drift test ("every `engagement_ui`/`artifacts_api` mutation has a machine counterpart").
  It would fail today on exactly the two routes above, i.e. it encodes a policy decision this branch is
  not making. The compensating control is the classification guard in
  `tests/test_scribble_machine_tenancy.py`, which does fail closed on a new machine route.
- ~~**Residual: a unicode `filename` can still overrun `NAME_MAX`.**~~ **CLOSED in review round 2** —
  and it should not have been listed here at all, because `docs/SCRIBBLE.md` was simultaneously telling
  readers a long name "is `ENAMETOOLONG` on write rather than a truncated column", i.e. shipping a doc that
  asserted the 500 was handled. `artifacts_storage.save_bytes` now bounds the basename after
  `secure_filename` (extension preserved) — the layer that knows the final name, which fixes the cookie
  upload path too — and the 222 API cap stays as the fast 400. See round 2, finding 5.
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

## Red-then-green — review round 2

Same rule as above: every guard was broken, watched fail, and restored. Output trimmed to the assertion
lines; the edit that produced each RED is stated so it can be reproduced exactly.

### 25. BLOCK · deleting a finding must clear ALL SIX referring columns, not two

Dropped the `clear_finding_referrers(db, [finding.id])` call from `findings_service.delete_finding` (the
state the branch was in when reviewed):

```
--- RED ---
E   AssertionError: None
    assert 500 == 200
sqlite3.IntegrityError: FOREIGN KEY constraint failed
  File ".../scribble/api_pat.py", line 1909, in scribble_delete_finding
sqlalchemy.exc.IntegrityError: (sqlite3.IntegrityError) FOREIGN KEY constraint failed
[SQL: DELETE FROM scribble_findings WHERE scribble_findings.id = ?]
FAILED …::test_delete_finding_clears_every_row_that_references_it - AssertionError: None
1 failed in 1.26s

--- GREEN (call restored) ---
1 passed
```

`ForeignKeyViolation` on prod Postgres, and note WHAT dies with it: the transaction rolls back, so the
finding is not deleted and the audit row is lost too. The reviewer reproduced this for a `CollabDoc`, a
checklist link and a per-finding `VariableValue` independently; the test seeds all of them at once, plus a
tag, an artifact and a child, so a fix that handles some-but-not-all cannot pass it.

### 26. BLOCK · the ENUMERATION guard (this is the one that makes the next referrer loud)

Removed `CollabDoc` from `_FINDING_OWNED_STATE` — i.e. simulated exactly the omission that shipped:

```
--- RED ---
E   AssertionError: a column referencing scribble_findings.id has no delete disposition:
    {('scribble_collab_docs', 'finding_id')} — classify it in findings_service (DELETE with the finding,
    NULL the link, or document who already handles it) or deleting a finding will 500 on Postgres
FAILED …::test_every_column_referencing_a_finding_has_a_declared_delete_disposition
1 failed in 0.11s

--- GREEN ---
1 passed
```

0.11s, no app boot, and it names the column. This is the check the branch was missing: round 1 fixed
`parent_id` and wrote a test for `parent_id`, and nothing anywhere asked "what else points at this table?"

### 27. BLOCK (same class) · the ENGAGEMENT delete had the same gap

Put `engagement_ui.engagement_delete` back to `findings_service.flatten_nesting(db, engagement)` (round 1's
fix — parent links only) instead of `prepare_engagement_delete`:

```
--- RED ---
E   assert 500 == 302
sqlite3.IntegrityError: FOREIGN KEY constraint failed
[SQL: DELETE FROM scribble_findings WHERE scribble_findings.id = ?]
FAILED tests/test_board.py::test_engagement_delete_survives_rows_that_reference_a_finding_from_OUTSIDE_the_cascade
1 failed in 2.19s

--- GREEN ---
1 passed
```

Worth stating plainly because it is the trap: round 1's
`test_engagement_delete_survives_a_promoted_parent_child_cluster` was green throughout the RED above. A test
that certifies one member of a set reads exactly like a test that certifies the set.

### 28. BLOCK · a non-doc `content_json` value must be REFUSED, not stored as an empty doc

Removed the `_non_doc_blocks_error` check from `_patch_content_blocks`:

```
--- RED ---
E   AssertionError: {'analyst_notes': None, 'artifacts': [], 'category': None, 'children': [], ...}
    assert 200 == 400
FAILED …::test_patch_refuses_a_non_doc_block_INSTEAD_of_emptying_the_prose
FAILED …::test_patch_rejects_malformed_input[body12]  # {"content_json": {"description": "Updated …"}}
FAILED …::test_patch_rejects_malformed_input[body13]  # {"description": {"type": "paragraph", …}}
FAILED …::test_patch_rejects_malformed_input[body14]  # {"impact": 42}
FAILED …::test_patch_rejects_malformed_input[body15]  # {"description": ["a", "b"]}
FAILED …::test_patch_rejects_malformed_input[body16]  # {"description": None}
6 failed

--- GREEN ---
7 passed
```

The RED here is the whole finding: **200**, and the stored block is `{"type": "doc", "content": []}` where the
author's write-up was. The same test asserts the cookie twin 400s on the identical body, so the two surfaces
are compared rather than assumed equal.

### 29. …and the same check on the CREATE routes (not reported — same class, one route over)

Removed both create-side `_non_doc_blocks_error` calls:

```
--- RED ---
E   assert (201 == 400)
FAILED …::test_create_routes_refuse_a_non_doc_block_too
1 failed in 1.31s

--- GREEN ---
1 passed
```

A 201 for prose that was never stored is the same silent success as a 200. Fixed without waiting for a third
review round to report it — the `_COLUMN_MAX_LEN` lesson from round 1, applied prospectively.

### 30. CONCERN · the id-list cap, and the ONE-query pre-check

Two separate reverts, because the finding has two halves and either alone leaves the amplification.

(a) Removed both `_BULK_ID_LIST_MAX` checks:

```
--- RED ---
E   AssertionError: {'detail': 'finding not found', 'error': 'not_found'}
    assert 404 == 400
FAILED …::test_bulk_move_refuses_an_unbounded_id_list_and_pre_checks_in_ONE_query
1 failed in 1.58s
```

(b) Restored the per-id `db.get` pre-check loop:

```
--- RED ---
E   AssertionError: 21 SELECTs on scribble_findings, expected 1
    assert 21 == 1
     +  where 21 = len(['SELECT scribble_findings.id … WHERE scribble_findings.id = ?', ...])
FAILED …::test_bulk_move_refuses_an_unbounded_id_list_and_pre_checks_in_ONE_query
1 failed in 1.40s

--- GREEN (both restored) ---
1 passed in 1.94s
```

**🔴 The first version of that assertion was vacuous and this transcript is why it is not.** It posted 50
ids that did not exist and asserted one SELECT — which the per-id loop ALSO satisfies, because it returned on
the first missing id. Reverting the fix left the test green. The payload is now 20 REAL ids followed by one
missing, so the loop must walk all 21 before it can refuse: 21 SELECTs vs 1. Exactly the failure mode the
branch's own round-1 note about a 512-character cap describes — a guard checked only on the side where it
cannot fail.

### 31. CONCERN · a NUL byte is escaped at the boundary, not passed to the column

Neutered `_nul_safe` to return its input unchanged:

```
--- RED ---
E   AssertionError: scan banner
    assert '\x00' not in 'scan\x00banner'
FAILED …::test_patch_escapes_a_NUL_byte_that_postgres_would_refuse[title]
FAILED …::test_patch_escapes_a_NUL_byte_that_postgres_would_refuse[analyst_notes]
FAILED …::test_patch_escapes_a_NUL_byte_that_postgres_would_refuse[target_host]
3 failed in 2.91s

--- GREEN ---
3 passed
```

Note what the RED actually shows: **200, value stored with the NUL intact** — which is what SQLite does. On
Postgres the same request is a 500 (`psycopg`: "A string literal cannot contain NUL (0x00) characters"), and
no test in this repo can show that, which is precisely why the assertion is on the STORED value rather than
on the status code.

### 32. CONCERN · a unicode filename that `secure_filename` EXPANDS must still store

Reverted `save_bytes` to `safe_name = secure_filename(filename) or "artifact"` (dropping `_bounded_name`):

```
--- RED ---
E   AssertionError: None
    assert 500 == 201
OSError: [Errno 36] File name too long: '…/artifacts/1/58fa6e64…_121212…12.png'
FAILED …::test_artifact_upload_stores_a_unicode_filename_that_secure_filename_EXPANDS
1 failed in 1.29s

--- GREEN ---
1 passed
```

The name in that path is the NFKD expansion of `"½" * 200`: 204 characters in, 404 out, one `.png`. The 222
API cap accepted it because the cap counts what the CALLER sent — which is why the bound belongs in
`save_bytes`, where the final name is known, and why the cookie upload path was equally exposed.

### 33. the engagement-side referrer nobody reported (found by asking the same question one table over)

Removed the `_ENGAGEMENT_UNCASCADED` sweep from `prepare_engagement_delete`:

```
--- RED ---
E   assert 500 == 302
sqlite3.IntegrityError: FOREIGN KEY constraint failed
FAILED tests/test_board.py::test_engagement_delete_clears_the_one_referrer_no_relationship_cascades
1 failed in 1.29s

--- GREEN ---
1 passed
```

One `ReportRender` row and the engagement cannot be deleted. Latent — nothing writes that table yet — so this
is a bug fixed before it had a chance to be reported, which is the only version of this class that costs
nothing.

### 34. …and its enumeration guard

Set `_ENGAGEMENT_UNCASCADED = ()`:

```
--- RED ---
E   AssertionError: a column referencing scribble_engagements.id is neither cascade-covered nor cleared by
    hand: {('scribble_report_renders', 'engagement_id')} — give it a relationship with
    cascade='all, delete-orphan' or add it to findings_service._ENGAGEMENT_UNCASCADED, or deleting an
    engagement will 500
FAILED tests/test_board.py::test_every_column_referencing_an_engagement_is_cascaded_or_declared
1 failed in 0.18s

--- GREEN ---
1 passed
```

`cascaded` is derived from `Engagement.__mapper__.relationships` (which of them cascade `delete-orphan`), not
from a list of table names, so adding a relationship is enough to satisfy it and adding a bare FK is not.
