# Plan: feat/vector-machine-api

- **Branch:** `feat/vector-machine-api`  (worktree: `.claude/worktrees/vector-machine`, off `main`)
- **PR:** https://github.com/ShyftXero/lotek-extensions/pull/21
- **Status:** 🟢 ready to merge

## Purpose
Give Vector a PAT/Bearer **machine API** at `/vector/machine` so host tools (an agent on a personal access
token) can build and export attack-path diagrams over REST — the diagram half of "an agent writes the
pentest report". Ports the work already shipped in **lotek PR #288** (`feat(api): introspective OpenAPI +
every extension PAT-drivable; retire MCP`), which edited only lotek's *vendored* copy of vector — this
closes that drift so vector's own repo owns its machine API.

## Done
- [x] `plans/` entry committed first
- [x] `vector/vector/host.py` — PAT capability accessors over `cfg.extras`, fail-closed 503, stamps
      `__lotek_scope__` (verbatim)
- [x] `vector/vector/api_schemas.py` — typed pydantic request bodies + `request_body` (verbatim)
- [x] `vector/vector/api_pat.py` — `machine_bp` + diagram CRUD + HTML export, **adapted** to `int` ids
- [x] `vector/vector/__init__.py` — import + register `machine_bp` at `<url_prefix>/machine`
- [x] `vector/lotek-extension.toml` — `[host] machine_prefix = "/machine"`
- [x] `vector/pyproject.toml` + `uv.lock` — `pydantic>=2`
- [x] `vector/tests/conftest.py` — `StubActor`, the three PAT hooks on an `app.pat` holder (matching the
      existing `app.holder` pattern), and a `pat_client` that blanks the session
- [x] `vector/tests/test_machine_api.py` — 23 tests
- [x] `uvx ruff check vector` clean + `cd vector && uv run python -m pytest` → **57 passed**
      (23 new, existing 34 unaffected)
- [x] Red/green proof: removing the DELETE route's `@host.require_scope` fails BOTH
      `test_every_machine_route_is_scope_gated`
      (`['/vector/machine/diagrams/<int:diagram_id>']`) and
      `test_write_routes_each_reject_a_read_only_token`, then restored

## Remaining
- [ ] nothing blocking on this branch
- [ ] **separate follow-up PR:** migrate vector to UUIDv7 PKs, then relax `_actor_owner_id()` to accept
      `uuid.UUID` (see below)

## Notes / gotchas

### Where the source came from (it moved mid-branch)
This port was started against lotek's PR-#288 worktree, which was **deleted while this branch was in
progress** — #288 merged at 2026-08-13T19:20Z and its worktree/branch were cleaned up. The files were then
taken from lotek's **merged `origin/main`** (`extensions/vector/vector/…`), which is the better source
anyway: it is post-merge and conflict-resolved. Re-verified at that point that the cream and registrar
files already committed on their branches still matched merged main — `host.py` and `registrar/api_pat.py`
byte-identical, and every other difference one of this port's own documented, deliberate changes.

### The upstream copy is AHEAD of this repo, on an unrelated migration
lotek's snapshot of vector has already been migrated to **UUIDv7 primary keys** (`Diagram.id`, `owner_id`,
`client_id`, `source_job_id` are all `Uuid`), and its `deps.current_actor_id()` guards on `uuid.UUID`.
**This repo's vector is still `Integer`-keyed.** So the vendored `api_pat.py` cannot be copied: its routes
are `<uuid:diagram_id>` and would never bind to an Integer-PK model.

This branch therefore **adapts** `api_pat.py` to `<int:diagram_id>`, matching the browser surface
(`vector/api.py` uses `<int:diagram_id>` throughout). The UUID migration is a **separate change** —
schema, seed, browser API, blueprint, standalone and existing rows — and does not belong in an API port.

### Known limitation this port INHERITS (not a regression)
CLAUDE.md's v2 contract requires UUIDv7 PKs, and lotek's core `User.id` **is** a UUID. Because this repo's
`Diagram.owner_id` is still `Integer`, a mounted vector cannot store a real lotek user id — today's
*browser* surface has exactly the same problem (`deps.current_actor_id()` returns `None` for a
`uuid.UUID`, so every mounted diagram is already unattributed). The machine API is written to degrade the
same way and **loudly**: `_actor_owner_id()` accepts an int and logs a warning for any other type rather
than silently binding a UUID into an Integer column. A test pins this behaviour so it is documented rather
than hidden.

**Follow-up (recommended, separate PR): migrate vector to UUIDv7 PKs**, then relax `_actor_owner_id()` to
accept `uuid.UUID`. Until then, PAT-created diagrams mounted in lotek are owner-NULL (admin-visible only),
which is the pre-existing mounted behaviour and not something this branch introduces.

### Other
- Tightened one inconsistency while adapting: the vendored `_visible_stmt()` filters non-admins on
  `owner_id == uid`, which SQLAlchemy renders as `owner_id IS NULL` when `uid` is None — so a principal
  with no id would LIST null-owner rows that `_load_visible_or_none()` correctly 404s. The adapted version
  returns builtin-only in that case, so list and get agree.
- Vector's conftest builds the app through `vector.standalone.create_app` and hangs a mutable `holder` off
  it; the PAT stubs follow that same pattern (`app.pat`).

<!-- Lifecycle: create + commit this FIRST thing when cutting the branch; keep it current; KEEP it —
     it merges to main with the branch as a durable record (since 2026-07-28; see CLAUDE.md
     "Per-branch plan"). -->
