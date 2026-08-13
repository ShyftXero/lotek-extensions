# Plan: feat/cream-machine-api

- **Branch:** `feat/cream-machine-api`  (worktree: `.claude/worktrees/cream-machine`, off `main`)
- **PR:** not opened yet
- **Status:** 🟢 ready to merge

## Purpose
Give Cream a PAT/Bearer **machine API** at `/cream/machine` so host tools (an agent on a personal access
token) can DRAFT invoices/quotes over REST. Ports the work already shipped in **lotek PR #288**
(`feat(api): introspective OpenAPI + every extension PAT-drivable; retire MCP`), which edited only lotek's
*vendored* copy of cream — this closes that drift so cream's own repo owns its machine API.

Finalization stays human-only: this surface exposes DRAFTING only, with **no `/issue` and no `/void`**.

## Done
- [x] `plans/` entry committed first
- [x] `cream/cream/host.py` — PAT capability accessors over `cfg.extras`, fail-closed 503, stamps
      `__lotek_scope__` (copied verbatim from lotek's vendored copy)
- [x] `cream/cream/api_schemas.py` — typed pydantic request bodies + `request_body`. Extended past the
      vendored copy to document the create fields this port actually honours (brand-default overrides,
      `client_id`) and the line-item `detail`/`unit`.
- [x] `cream/cream/api_pat.py` — `machine_bp` + routes (drafting only), reconciled against the monorepo
- [x] `cream/cream/__init__.py` — import + register `machine_bp` at `<url_prefix>/machine`
- [x] `cream/lotek-extension.toml` — `[host] machine_prefix = "/machine"`
- [x] `cream/pyproject.toml` + `uv.lock` — `pydantic>=2` (a real new runtime dep: `api_schemas` imports it)
- [x] `cream/tests/conftest.py` — `StubActor` + the three PAT hooks, and a `pat_client` fixture that
      blanks the session so a request has a real machine request's shape
- [x] `cream/tests/test_machine_api.py` — 17 tests: scope gate, PAT-actor attribution, operator gate,
      read scoping, fail-closed 503, drafting end to end, and the two omissions
- [x] `uvx ruff check cream` clean + `cd cream && uv run python -m pytest` → **130 passed**
- [x] Red/green proof: removing one `@host.require_scope` makes
      `test_every_machine_route_is_scope_gated` fail with
      `machine routes missing require_scope: ['/cream/machine/documents']`, then restored

## Remaining
- [ ] nothing blocking

## Notes / gotchas
- **Source is lotek's vendored copy, which is STALE against this monorepo.** Vendored cream lacks
  `markup.py`, `money.py`, `viewmodel.py` and its `service.py`/`api.py`/`models.py` differ by hundreds of
  lines. So: `host.py` and `api_schemas.py` are copied verbatim, but `api_pat.py` is **reconciled** against
  the monorepo's actual `service`/`models` signatures rather than copied blind.
- Monorepo `cream.service.totals()` returns a **`TotalsView` dataclass of `Decimal`s** (not a dict).
  Flask's default JSON provider serializes both (dataclass -> `asdict`, `Decimal` -> str), so
  `jsonify` works, but the emitted `totals` shape differs from the vendored copy's. Same for
  `Suggestion.unit_price`.
- Tenancy comes from the **PAT actor** (`host.actor()`), never `cream.deps.current_actor_*` — those are
  session-only and are `None` on a PAT request, which would orphan `owner_id`.
- `host_can_operate_on` / `host_visible_engagement_ids` are principal-based hooks and are already wired by
  `cream/tests/conftest.py`, so only the three PAT hooks are new there.
- The conftest's `require_pat_scope` stub **really enforces scope** (unlike scribble's no-op passthrough),
  so the scope-gate test can assert a read-only token gets 403 on a write route. Whether a token *has* a
  scope is the host's concern; which scope each route *declares* is cream's, and only a real gate proves it.
- Two deliberate departures from the vendored copy, both to match the monorepo's *current* browser surface
  (which #288's own parity note asks extensions to mirror):
  1. the document/line JSON is `cream.api._doc_json` / `_line_json`, not a machine-local copy — one wire
     shape, and money crosses both boundaries through `cream.money` rather than as coerced `Decimal`s;
  2. create applies the brand defaults (currency, tax label/rate, quote RoE), so an agent-drafted document
     renders like a dashboard-drafted one. Checked first that `service.update_document`'s allowlist cannot
     reach `number` or `status` — a PAT still cannot forge an invoice number or issue anything.
- `cream/tests/conftest.py` returns the `visible_engagement_ids` hook's value **as-is** but *calls*
  `can_operate_on`. A test that sets the former to a lambda silently scopes nothing.
- The mounted-behaviour proof lives lotek-side in #288's `tests/test_openapi_introspection.py`; lotek
  already carries a working copy of this code, so no re-vendor follows this merge.

<!-- Lifecycle: create + commit this FIRST thing when cutting the branch; keep it current; KEEP it —
     it merges to main with the branch as a durable record (since 2026-07-28; see CLAUDE.md
     "Per-branch plan"). -->
