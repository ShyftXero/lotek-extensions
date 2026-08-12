# Plan: fix/scribble-machine-tenancy

- **Branch:** `fix/scribble-machine-tenancy`  (worktree: `.claude/worktrees/scribble-machine-tenancy`, off `main`)
- **PR:** not opened yet
- **Status:** 🟢 ready to merge

## Purpose

`plans/fix-scribble-tenancy-gate.md` closed the systemic cross-tenant IDOR on `bp`/`api_bp` and left one
follow-up: the PAT/machine blueprint (`scribble/api_pat.py`) has the SAME class of gap. This branch closes
that follow-up — and, because the brief was *"find every instance of this class in the reporting
framework"*, every other instance found by the sweep, so the class is closed rather than one route.

**The class**, stated once: *a route reaches engagement/client data without asking the host whether the
caller's grant covers that client.* It has three shapes, and the previous branch only closed the first:

1. **read/write by id** — the URL names an engagement (or a child of one); the route loads it and acts.
   Closed on `bp`/`api_bp` by the `authz.register_gate` `before_request`; **NOT** on `machine_bp`.
2. **client id from the request** — the caller *supplies* the client an engagement is created under (or
   moved to). There is no row to gate on yet, so the view-arg gate structurally can't see it.
3. **unscoped list** — no id at all: the route enumerates every engagement in the database. This one needs
   no id-guessing, which makes it the cheapest of the three to exploit and the easiest to overlook (both
   list routes were sitting in the previous branch's `_NON_SCOPED_ENDPOINTS` allowlist, correctly
   classified as "carries no engagement view-arg" and wrongly read as "therefore not engagement data").

## The sweep (what was audited, and the verdict)

| Surface | Verdict |
|---|---|
| `scribble/api_pat.py` `machine_bp` — 8 routes | **3 instances** (below) |
| `scribble/blueprint.py`, `scribble/engagement_ui.py` — dashboard/list/create/edit | **3 instances** (below) |
| `scribble/{artifacts,checklists,autosave,templating,report_html,report_docx}_api.py`, `collab/*` | clean — every engagement-scoped route carries a recognized view arg and is covered by `authz.register_gate`; the two body-scoped ones (`create_artifact`, `templating_preview`) call `authorize_engagement_view` directly |
| `scribble/library_ui.py`, `scribble/assessment_types_ui.py`, `checklists_api.py`'s template routes | clean by construction — `VulnerabilityTemplate`/`AssessmentType`/`ChecklistTemplate` are library-wide tables shared across all tenants, with no engagement axis |
| `cream/` (the deliverable engine — the other half of the reporting framework) | **clean.** `_load()` gates every by-id read/write (`visible_engagement_ids` + `can_operate_on`), both list routes filter on `visible_engagement_ids`, and `create_document` calls `_require_operator(eid)` on the BODY-supplied engagement id before any write. This is the shape scribble is being brought up to. |
| `registrar/` | out of scope (not the reporting framework) — glanced only |

### The six instances

**`machine_bp` — no tenancy check of any kind (shape 1 + 2):**

1. `scribble_add_finding` — `POST /machine/engagements/<id>/findings`: loads the engagement by id, writes a
   finding into it. Any valid `write`-scoped PAT could author findings into any client's engagement.
2. `scribble_promote_job` — `POST /machine/engagements/<id>/promote-job/<job_id>`: the *job* side is
   properly gated (the host's `get_job`/`list_findings` apply `user_can_view_job` internally), the
   *engagement* side was not. Both directions leak: it writes into another tenant's report, AND it copies
   the caller's own scan findings into a report the caller cannot read — data crossing the boundary
   outward, which is the worse half and the easier one to miss.
3. `scribble_create_engagement` — `POST /machine/engagements`: `client_id` comes from the request body and
   was written verbatim.

**cookie `bp` — sitting in the previous branch's non-scoped allowlist (shape 2 + 3):**

4. `scribble.dashboard` — the 10 most recent engagements *and* global counts, across every client.
5. `scribble.engagements` — the full engagement list, across every client.
6. `engagement_new` (POST) and `_apply_engagement_form` (the edit route) — `client_id` from the form, with
   no grant check on the client being assigned. Create-under-a-foreign-client, and (edit) *move* an
   engagement you legitimately hold onto a client you don't, or off yours onto someone else's.

## Two things the sweep turned up that are NOT tenancy holes but must be fixed for the fix to be usable

- **`client_id` was parsed with `_opt_int`.** Under lotek v2 a client id is a UUID, so a machine caller
  literally could not pass one (`int("0198…")` → 400). Gating a field nobody can set would have shipped a
  route that can only fail; `_opt_host_id` now accepts either shape, matching the `SoftHostId` column the
  value lands in.
- **A client-less engagement is unreachable, and always was.** `can_view_client(None, actor)` is False by
  the host's own contract, so an engagement with no `client_id` 404s for *everyone* (this is already true
  on `main` for the board and the report). Creating one is a silent no-op dressed as a success. So both
  create paths now REQUIRE a viewable client when a host is mounted, rather than cheerfully creating data
  nobody — including its creator — can ever open. Standalone Scribble (no host bundle) is unchanged.

## Done

- [x] Full sweep of the reporting framework (table above).
- [x] `scribble/authz.py`: `host_is_mounted` / `can_view_client_id` / `can_view_engagement` (non-aborting
      predicates, explicit actor) + `filter_visible_engagements` (per-call cache keyed on `client_id`, so
      a list costs one host answer per distinct client rather than per row). `authorize_engagement_view`
      is now the thin aborting wrapper over the same predicate — one policy, three call shapes.
- [x] `scribble/api_pat.py`: `add_finding` + `promote-job` check `can_view_engagement(engagement,
      host.actor())`; `create_engagement` checks the body's `client_id` and requires one when mounted.
      `_opt_host_id` replaces `_opt_int` for `client_id` (int OR UUID). `add_finding` now authorizes
      BEFORE parsing the body, so the refusal is identical whatever the body says.
- [x] `scribble/blueprint.py`: the dashboard list AND its stat tiles derive from the visible set.
- [x] `scribble/engagement_ui.py`: the engagement list filters; `_resolve_client` (shared by create and
      edit) enforces the three client rules; `_viewable_clients` scopes the form's client `<select>`,
      which was rendering the host's entire client table. Both form templates drop the "new client name"
      field when mounted (`scribble_host_mounted`), since the server now refuses it.
- [x] `tests/conftest.py`: the stub `can_view_client` takes either principal shape — it would have
      `AttributeError`d on `StubActor.role` (a plain string) the first time a machine route asked it.
- [x] Tests: `tests/test_scribble_machine_tenancy.py` (13) — a `machine_bp` route-classification guard, a
      real-request denial sweep over every engagement-scoped machine route, per-route DENY→ALLOW with
      "and nothing was written" assertions, and the four create-route client cases including the UUID one.
      `tests/test_scribble_list_tenancy.py` (12) — list/dashboard/counts scoping, the client picker, the
      create rules, the edit MOVE case, and standalone-unchanged.
- [x] Existing machine tests updated to create under a granted client (`_engagement(client, stub_host)`),
      and `test_scribble_tenancy_gate.py`'s edit case to keep naming its client. The `_NON_SCOPED_ENDPOINTS`
      comment now says what that list does and does not claim — misreading it is what left these open.

- [x] Red→green proven for every new guard, by disabling the code they guard:
      - machine checks removed (`can_view_engagement` ×2 + the create-route `client_id` block) →
        **7 fail** in `test_scribble_machine_tenancy.py` (the route sweep, all three `add_finding` denial
        cases, `promote_job`'s, and both create denials); restored → **14 pass**.
      - list filtering + `_resolve_client`'s mounted branch removed → **8 fail** in
        `test_scribble_list_tenancy.py` (both lists, the counts, all three create rules, both edit-move
        cases); restored → **12 pass**.
      - all five tenancy files together after restore: green (`test_scribble_list_tenancy`,
        `test_scribble_machine_tenancy`, `test_scribble_tenancy_gate`, `test_scribble_report_authz`,
        `test_report_docx_authz`).
- [x] `uvx ruff check scribble tests` clean; `pyrefly check` clean on every changed file (0 errors).
- [x] Full suite: **526 tests, 506 passed, 9 failed, 11 skipped**. All 9 are `tests/test_skill.py` and
      pre-existing: the `skill/` directory does not exist anywhere in this repo, so they fail identically
      on `main` (documented in `plans/fix-scribble-docx-report-tenancy.md`).

## Remaining

- [ ] Re-vendor into lotek (`scripts/stage-extension.sh`) — after merge. The mounted-side proof
      (`lotek/tests/test_scribble_extension.py`) can only run once the snapshot is in lotek's tree.
- [ ] Host-side follow-up: a `visible_client_ids` hook would let both list routes scope in SQL instead of
      filtering in Python (see the adversarial-review note below).

## Adversarial review (2026-08-12) — verdict CONCERNS, one fix applied

- **FIXED (warning).** The machine denial sweep built its urls by substituting `<int:engagement_id>` into
  `rule.rule`. A converter change — the pending UUID PK migration, say — would leave the placeholder in
  the url, Werkzeug would 404 at the routing layer, and a sweep asserting 404 would keep passing while
  exercising nothing. Now built with `url_for`, plus the companion ALLOW sweep that was missing: a
  denial-only sweep also passes if every url is malformed or the blueprint stops being mounted.
- **ACCEPTED (warning).** `dashboard` was `LIMIT 10`; it now reads every engagement row to filter and to
  derive the counts. The seam gives a per-client PREDICATE (`can_view_client`), not an enumerable set, so
  the scoping cannot be pushed into SQL — one host answer per DISTINCT client (cached per call), but a
  full engagement scan per dashboard load. The fix is host-side: a `visible_client_ids` hook (cream
  already gets `visible_engagement_ids`), which would turn both list routes into a `WHERE client_id IN`.
- **NOTE.** The dashboard's Clients tile changed meaning: clients you hold engagements under, not rows in
  the client table. Deliberate — the old number told a single-client member how many clients exist.
- **NOTE.** `_resolve_client` refuses two ways: `abort(404)` for a foreign client id (no existence
  oracle) and a returned message for the two form-level rules (which re-render with an explanation).

## Notes / gotchas

- **Which actor.** `authorize_engagement_view` reads `deps.current_actor()` (the browser session user).
  A machine request has no session — its principal is `host.actor()` (`extras['pat_actor']`). The host's
  `can_view_client` is duck-typed on `.id` precisely so it can take either (see `make_can_view_client`'s
  docstring), so the machine routes pass the PAT actor explicitly instead of reusing the UI wrapper.
- **Why not extend `register_gate` to `machine_bp`.** The gate resolves `current_actor()`; on a PAT
  request that is None → every machine route would 404. The predicate is shared; the actor lookup is not.
- **404, not 403**, everywhere — including for an unviewable *client* id on the create routes. Same
  no-existence-oracle posture the rest of the module already carries.
- **Existing machine tests create client-less engagements**, which the new create rule refuses when a host
  is mounted; they are updated to create under a client the actor holds, which is what production looks
  like. That is the harness being brought up to prod, not the fix being loosened around it.
