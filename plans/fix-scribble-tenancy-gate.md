# Plan: fix/scribble-tenancy-gate

- **Branch:** `fix/scribble-tenancy-gate`  (worktree: `.claude/worktrees/scribble-tenancy-gate`, off `main`)
- **PR:** https://github.com/ShyftXero/lotek-extensions/pull/11
- **Status:** 🟢 done, ready for merge

## Purpose

Only the HTML/docx report routes (`report_html_api.py`/`report_docx_api.py`) called the host-delegated
`can_view_client` tenancy check (`authorize_engagement_view`, formerly `_authorize_engagement_view`).
Every OTHER engagement-scoped route on the `bp`/`api_bp` blueprints — `engagement_ui.py`'s board/edit/
delete/add-finding/reorder, `artifacts_api.py`'s artifact CRUD, `checklists_api.py`'s assignment routes,
`autosave_api.py`/`collab/*`'s per-block routes — did a bare `db.get(...)` with no tenancy check at all,
and there was no blueprint-wide gate to catch a route module that forgot to call it. Any authenticated
actor could READ and WRITE another client's engagement data (findings, evidence artifacts, checklists,
content blocks) by enumerating ids. This closes it systemically instead of one route at a time.

## Done

- [x] Mapped every route on `bp`+`api_bp` (`scribble/blueprint.py`, `scribble/api.py`,
      `scribble/engagement_ui.py`, `scribble/artifacts_api.py`, `scribble/checklists_api.py`,
      `scribble/library_ui.py`, `scribble/assessment_types_ui.py`, `scribble/autosave_api.py`,
      `scribble/collab/crdt.py`, `scribble/collab/presence.py`, `scribble/templating_api.py`,
      `scribble/report_html_api.py`, `scribble/report_docx_api.py`) and classified each as
      engagement-scoped or not — see the PR body for the full inventory.
- [x] `scribble/authz.py` (new): moved `authorize_engagement_view` out of `report_html_api.py` (no
      cycle — `report_docx_api.py`/`templating_api.py`/`artifacts_api.py` now import it from here
      directly). Added the resolver map (`_DIRECT_KEYS`/`_CHILD_RESOLVERS`) and a fail-closed
      `before_request` gate (`register_gate`) covering every view-arg-identified engagement/child id:
      `engagement_id`/`eid` (direct), `finding_id`/`group_id`/`artifact_id`/`cid` (direct child FK),
      `iid` (two-hop: `EngagementChecklistItem.checklist.engagement_id`).
- [x] Wired `register_gate(api_bp, bp)` into `scribble/__init__.py::_wire_feature_routes`, guarded by the
      same `_FEATURE_ROUTES_WIRED` flag as every route registration — NOT on `machine_bp` (its own PAT
      `before_request` gate + a different, host-decided tenancy story; see PR body's note on the gap
      that remains there).
- [x] Two routes take their target `engagement_id` from the request BODY, not a URL view-arg
      (`artifacts_api.create_artifact`, `templating_api.templating_preview`) — the generic gate can't
      reach them by design. Fixed both with a direct `authorize_engagement_view(engagement)` call,
      mirroring the pre-existing report-route pattern, checked BEFORE any bytes are written to disk
      (artifact upload) or any finding content is rendered back (preview).
- [x] Coverage test `tests/test_scribble_tenancy_gate.py`: derives the route list from `app.url_map`,
      asserts every route is either declared non-scoped or carries a recognized id (fails on an
      unclassified new route), fires a real HTTP request as a non-member for every scoped route and
      asserts 404, and the companion positive (authorized actor, must never see the gate's 404). Plus
      explicit per-class DENY→ALLOW tests for the named routes (board/edit/delete/add_finding/reorder/
      artifact_raw) and the two body-scoped routes. 14 tests in this file; 32 when run together with
      the two existing report-authz files that share the same primitive (`test_scribble_report_authz.py`
      + `test_report_docx_authz.py`), used as the combined red→green harness below.
- [x] Red→green proven three times over (see PR body): the blueprint gate disabled → every deny test
      fails 200/302 instead of 404; each body-scoped inline call disabled → its own test fails the same
      way; both restored → green.
- [x] `uv run ruff check` + `uv run pyrefly check` clean on every changed file.
- [x] Found + fixed a related, pre-existing flake while running the full suite for this branch:
      `templating_api.py`'s idempotent-registration guard tracked already-wired `Blueprint` objects in a
      module-level `set[id(api_bp)]`. `tests/test_templating.py`'s `preview_client` fixture builds a
      throwaway `Blueprint("scribble_api", ...)` per test; across a long full-suite run one of those
      objects can be garbage-collected and have its address reused by a LATER test's throwaway
      blueprint, so `register()` silently skipped attaching `/preview` to that later, unrelated
      blueprint. Confirmed pre-existing and independent of this branch's own diff (stashed everything,
      ran the bare suite against `origin/main` — clean 2/2; with this branch's diff but BEFORE this
      specific fix — dirty 3/5, always the same `test_templating.py` cases, one of which sends no
      `engagement_id` at all and hits a `400` that exists identically on `main`, before any line this
      branch added ever runs, so the `404` it got instead can only mean the route wasn't registered on
      that app at all). Fixed by switching to the SAME attribute-on-the-blueprint idiom
      `artifacts_api.py`/`report_docx_api.py` already use elsewhere in this package (immune to id
      reuse). Re-verified clean 5/5 full-suite runs after the fix.
- [x] Full suite (final, post-fix): 501 collected, 492 passed / 9 pre-existing `test_skill.py` failures
      (documented in `plans/fix-scribble-docx-report-tenancy.md`, unrelated to this branch), 0 skipped —
      reproduced identically across 5 consecutive full runs.

## Remaining

- [ ] Follow-up (not this branch): `scribble/api_pat.py` (machine/PAT blueprint) has the SAME class of
      gap on two routes — `scribble_add_finding`/`scribble_promote_job` load an `Engagement` by id with
      no check that the caller's client grant covers it (only the promoted-FROM job's tenancy is
      checked, never the promoted-INTO engagement's client). Out of scope here per the task brief
      (machine_bp has its own PAT auth model); noted for whoever owns that blueprint's tenancy story.
- [ ] Once merged, re-vendor into lotek (`scripts/stage-extension.sh`).

## Notes / gotchas

- The gate's resolution is driven purely by `request.view_args` key NAMES (`engagement_id`/`eid`/
  `finding_id`/`group_id`/`artifact_id`/`cid`/`iid`), not per-endpoint — a brand-new route reusing one
  of those names is covered for free; a brand-new id concept is NOT, and
  `test_every_scribble_route_is_classified` fails loudly on it until it's explicitly classified.
- `authorize_engagement_view` already failed OPEN standalone (`cfg.extras.get("host")` absent) before
  this branch — the gate inherits that, so standalone Scribble (no host bundle) is unaffected. Verified:
  every pre-existing test file that doesn't wire the `stub_host` fixture passes unchanged.
- The real websocket route (`collab/crdt.py::collab_ws`) IS covered by the gate (it's a normal
  Flask-Sock-registered route on `bp`, and `before_request` runs before the view regardless) but can't
  be exercised via the plain test client (a non-upgrade request 400s at Werkzeug's routing layer before
  reaching any `before_request` hook) — same reason the route itself is `# pragma: no cover`.
