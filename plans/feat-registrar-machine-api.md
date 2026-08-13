# Plan: feat/registrar-machine-api

- **Branch:** `feat/registrar-machine-api`  (worktree: `.claude/worktrees/registrar-machine`, off `main`)
- **PR:** not opened yet
- **Status:** 🟢 ready to merge

## Purpose
Give Registrar a PAT/Bearer **machine API** at `/registrar/machine` so host tools (an agent on a personal
access token) can read infrastructure and drive actions over REST. Ports the work already shipped in
**lotek PR #288** (`feat(api): introspective OpenAPI + every extension PAT-drivable; retire MCP`), which
edited only lotek's *vendored* copy of registrar — this closes that drift so registrar's own repo owns its
machine API.

**A PAT stages; a human approves** (INV-EXT-02). Confirm-tier verbs are staged (202) and there is no
`/approve` route on this surface at all.

## Done
- [x] `plans/` entry committed first
- [x] `registrar/registrar/host.py` — PAT capability accessors over `cfg.extras`, fail-closed 503, stamps
      `__lotek_scope__` (verbatim)
- [x] `registrar/registrar/api_schemas.py` — typed pydantic `ActionRequest` + `request_body`. One fix on
      top of the vendored copy: it documented `'domain.checkout'` as the example verb, which is not a
      registrar verb at all — replaced with the real direct/confirm verb lists, since this schema is what
      an agent reads out of the OpenAPI spec.
- [x] `registrar/registrar/api_pat.py` — `machine_bp` + routes (reads + `/action`, staging only), verbatim
- [x] `registrar/registrar/__init__.py` — import + register `machine_bp` at `<url_prefix>/machine`
- [x] `registrar/lotek-extension.toml` — `[host] machine_prefix = "/machine"`
- [x] `registrar/pyproject.toml` + new `uv.lock` — `pydantic>=2`
- [x] **`registrar/tests/` — a whole new test harness** (Registrar shipped no tests directory): mounted-app
      fixture, controllable host hooks, `StubActor`, a recording `AuditLog` for the host audit seam, a
      scope-enforcing `require_pat_scope`, and a `pat_client` that blanks the session AND marks the request
      non-interactive (which a token always is)
- [x] `registrar/tests/test_machine_api.py` — 19 tests
- [x] `uvx ruff check registrar` clean + `cd registrar && uv run python -m pytest` → **19 passed**
- [x] Red/green proof: removing one `@host.require_scope` makes
      `test_every_machine_route_is_scope_gated` fail with
      `machine routes missing require_scope: ['/registrar/machine/domains']`, then restored

## Remaining
- [ ] nothing blocking

## Notes / gotchas
- Registrar's vendored copy is otherwise IDENTICAL to this repo's (the only pre-existing difference is one
  word in a `drivers.py` docstring, where the vendored copy still says "fraction" for what this repo calls
  "scribble" — left alone, it is not this branch's business). So the three new files come over verbatim.
- `registrar.service.tier_of` is re-exported through `service.py` (it is defined in `drivers.py`), so the
  vendored `from registrar.service import … tier_of …` import binds here unchanged.
- `tier_of` **defaults to `Tier.confirm`** for an unrecognized verb, so an unknown verb is staged rather
  than executed — fail-closed, and worth a test.
- The service-level guard is the real one: `service.approve` raises `ApprovalDenied` when
  `is_interactive` is False, which is what a PAT is. The missing `/approve` route makes that explicit at
  the surface; both are tested (route absent, and the guard itself refuses a non-interactive caller).
- Verbs available for tests: direct = `list_nodes`, `list_records`; confirm = `create_node`,
  `destroy_node`, `upsert_record`, `register_domain`, `send_sms`.
- Pre-existing latent oddity, NOT touched: `get_node` is declared `Tier.direct` but `service._dispatch`
  has no branch for it, so it 400s. Out of scope for this branch; noted so it is not mistaken for a
  regression from this port.

<!-- Lifecycle: create + commit this FIRST thing when cutting the branch; keep it current; KEEP it —
     it merges to main with the branch as a durable record (since 2026-07-28; see CLAUDE.md
     "Per-branch plan"). -->
