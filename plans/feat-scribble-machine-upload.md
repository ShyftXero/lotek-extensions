# Plan: feat/scribble-machine-upload

- **Branch:** `feat/scribble-machine-upload`  (worktree: `.claude/worktrees/scribble-upload`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose
Scribble already has a machine API; this **extends** it with the piece "an agent writes the pentest report"
was missing: **evidence/screenshot upload** over a PAT. Also stamps `__lotek_scope__` so scribble's machine
routes appear in the host's generated OpenAPI spec at all, and declares its request bodies as typed
pydantic models.

Ports the scribble half of **lotek PR #288** (`feat(api): introspective OpenAPI + every extension
PAT-drivable; retire MCP`), which edited only lotek's vendored copy — this closes that drift so scribble's
own repo owns it.

## Done
- [ ] `plans/` entry committed first

## Remaining
- [ ] `scribble/scribble/host.py` — add `SCOPE_ATTR = "__lotek_scope__"` and stamp it in `require_scope`
      (without this, NONE of scribble's existing machine routes enter the generated spec)
- [ ] `scribble/scribble/api_schemas.py` — NEW: `request_body` + `CreateEngagementRequest`,
      `AddFindingRequest`, `UploadArtifactRequest`
- [ ] `scribble/scribble/api_pat.py` — `@request_body(...)` stamps + the new evidence-upload route
      `POST /engagements/<id>/artifacts`
- [ ] **Address the open review comment from #288** (see below)
- [ ] `scribble/pyproject.toml` — `pydantic>=2`
- [ ] `scribble/tests/test_machine_artifacts.py` — upload tests
- [ ] `uvx ruff check scribble` clean + `cd scribble && uv run python -m pytest` green

## Notes / gotchas

### The one review comment from #288 that applies to this repo
#288 got four Copilot review comments. Three are lotek-side (`src/app/api_v1.py`,
`src/app/api_schemas.py`, `src/app/openapi_gen.py` — stale docstring references and an OpenAPI `security`
nit) and are out of scope here. The fourth lands squarely on the code being ported:

> `extensions/scribble/scribble/api_pat.py:568` — For JSON/base64 uploads, the size cap is enforced only
> *after* `base64.b64decode`, so a very large base64 string (up to `MAX_CONTENT_LENGTH`) will still be
> fully buffered/decoded in memory before returning 413. Add a preflight length check on the base64 string
> to reject oversized payloads before decoding.

**Fixed here**, not carried over: the b64 string length is checked BEFORE `b64decode`. Base64 encodes 3
bytes as 4 chars, so any string longer than `4 * ceil(max/3)` cannot decode to something within the cap —
rejecting on that bound is exact (no false 413 for a legal payload) and avoids materializing the decode.
The post-decode check stays as the authoritative one.

### Provenance
Source is lotek's **merged `origin/main`** (`extensions/scribble/scribble/…`). #288 merged at
2026-08-13T19:20Z and its pre-merge worktree was deleted mid-session, so the merged tree is both the
available source and the better one (conflict-resolved).

Verified before copying that upstream's `api_pat.py` is a strict superset of this repo's: the only line
this repo has that upstream lacks is the `from scribble.deps import …` line, which upstream merely extends
with `get_config`. This repo's tenancy work (#11/#12/#13/#17) did not touch `api_pat.py` — #17 changed
`authz.py`/`blueprint.py`/`engagement_ui.py` — so a wholesale copy loses nothing.

### Security shape of the upload route (what the tests must pin)
- **Tenancy before any write**: `can_view_engagement(engagement, host.actor())` on the engagement from the
  URL (never a body-supplied id), checked before a single byte is stored. Missing and not-visible are the
  same 404 (no existence oracle).
- **Size cap**: 25 MiB, 413 past it, now with the preflight above.
- **Idempotency**: `idempotency_key` (body or `Idempotency-Key` header) makes a retry return the original
  artifact (200) instead of a duplicate.
- `scribble/lotek-extension.toml` already declares `[host] machine_prefix`, so no manifest change.

<!-- Lifecycle: create + commit this FIRST thing when cutting the branch; keep it current; KEEP it —
     it merges to main with the branch as a durable record (since 2026-07-28; see CLAUDE.md
     "Per-branch plan"). -->
