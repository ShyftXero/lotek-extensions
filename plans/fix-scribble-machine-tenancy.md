# Plan: fix/scribble-machine-tenancy

- **Branch:** `fix/scribble-machine-tenancy`  (worktree: `.claude/worktrees/scribble-machine-tenancy`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

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

## Remaining

- [ ] `scribble/authz.py`: `can_view_client_id` / `can_view_engagement` (non-aborting predicates) +
      `filter_visible_engagements`; `authorize_engagement_view` becomes the aborting wrapper.
- [ ] `scribble/api_pat.py`: the three routes + `_opt_host_id`.
- [ ] `scribble/blueprint.py` + `scribble/engagement_ui.py`: the three cookie-side instances.
- [ ] `tests/conftest.py`: the stub `can_view_client` must accept a PAT actor (`StubActor.role` is a
      string, not a `_StubRole`) — today it would `AttributeError`.
- [ ] Tests: machine-route DENY→ALLOW per route + a `machine_bp` route-classification guard (the same
      "a new route can't slip through unclassified" property the cookie blueprints already have);
      list/dashboard scoping; create/edit client-grant checks.
- [ ] Red→green transcript for every new guard.
- [ ] `uv run ruff check` + `uv run pyrefly check` + full suite.
- [ ] Re-vendor into lotek (`scripts/stage-extension.sh`) — after merge.

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
