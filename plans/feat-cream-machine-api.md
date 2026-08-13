# Plan: feat/cream-machine-api

- **Branch:** `feat/cream-machine-api`  (worktree: `.claude/worktrees/cream-machine`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose
Give Cream a PAT/Bearer **machine API** at `/cream/machine` so host tools (an agent on a personal access
token) can DRAFT invoices/quotes over REST. Ports the work already shipped in **lotek PR #288**
(`feat(api): introspective OpenAPI + every extension PAT-drivable; retire MCP`), which edited only lotek's
*vendored* copy of cream — this closes that drift so cream's own repo owns its machine API.

Finalization stays human-only: this surface exposes DRAFTING only, with **no `/issue` and no `/void`**.

## Done
- [ ] `plans/` entry committed first

## Remaining
- [ ] `cream/cream/host.py` — PAT capability accessors over `cfg.extras`, fail-closed 503, stamps
      `__lotek_scope__`
- [ ] `cream/cream/api_schemas.py` — typed pydantic request bodies + `request_body`
- [ ] `cream/cream/api_pat.py` — `machine_bp` + routes (drafting only)
- [ ] `cream/cream/__init__.py` — import + register `machine_bp` at `<url_prefix>/machine`
- [ ] `cream/lotek-extension.toml` — `[host] machine_prefix = "/machine"`
- [ ] `cream/tests/conftest.py` — add the PAT hooks (`pat_authenticate` / `pat_actor` /
      `require_pat_scope`) alongside the existing cookie hooks
- [ ] `cream/tests/test_machine_api.py` — scope gate, PAT-actor tenancy, confirm-tier omissions
- [ ] `uvx ruff check cream` clean + `cd cream && python -m pytest -q` green

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
  so the scope-gate test can assert a read-only token gets 403 on a write route.

<!-- Lifecycle: create + commit this FIRST thing when cutting the branch; keep it current; KEEP it —
     it merges to main with the branch as a durable record (since 2026-07-28; see CLAUDE.md
     "Per-branch plan"). -->
