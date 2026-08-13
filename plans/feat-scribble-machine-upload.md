# Plan: feat/scribble-machine-upload

- **Branch:** `feat/scribble-machine-upload`  (worktree: `.claude/worktrees/scribble-upload`, off `main`)
- **PR:** not opened yet
- **Status:** 🟢 ready to merge

## Purpose
Scribble already has a machine API; this **extends** it with the piece "an agent writes the pentest report"
was missing: **evidence/screenshot upload** over a PAT. Also stamps `__lotek_scope__` so scribble's machine
routes appear in the host's generated OpenAPI spec at all, and declares its request bodies as typed
pydantic models.

Ports the scribble half of **lotek PR #288** (`feat(api): introspective OpenAPI + every extension
PAT-drivable; retire MCP`), which edited only lotek's vendored copy — this closes that drift so scribble's
own repo owns it.

## Done
- [x] `plans/` entry committed first
- [x] `scribble/scribble/host.py` — `SCOPE_ATTR = "__lotek_scope__"` + stamped in `require_scope`
      (without this, NONE of scribble's existing machine routes enter the generated spec)
- [x] `scribble/scribble/api_schemas.py` — NEW: `request_body` + `CreateEngagementRequest`,
      `AddFindingRequest`, `UploadArtifactRequest`
- [x] `scribble/scribble/api_pat.py` — `@request_body(...)` stamps + the evidence-upload route
      `POST /engagements/<id>/artifacts`
- [x] The #288 review comment: verified the merged source already carries the preflight, and added the
      **test** that pins it (see below)
- [x] `scribble/pyproject.toml` + `uv.lock` — `pydantic>=2`
- [x] `scribble/tests/test_machine_artifacts.py` — 22 tests
- [x] `uvx ruff check scribble` clean
- [x] `cd scribble && uv run python -m pytest` → **550 passed, 10 failed** — all 10 in `tests/test_skill.py`
      and **pre-existing on `main`**: they require `scribble/skill/scribble-report-refine/`, which is not
      tracked in git at all (`git ls-tree origin/main -- scribble/skill` is empty), i.e. a machine-local
      directory. This branch touches nothing they read. `tests/test_machine_artifacts.py` → 22 passed.

## Remaining
- [ ] nothing blocking
- [ ] (unrelated, pre-existing) `tests/test_skill.py` depends on an untracked `scribble/skill/` directory,
      so it cannot pass from a clean clone. Worth either committing the skill or marking those tests
      skip-if-absent — a separate change.

## Notes / gotchas

### The one review comment from #288 that applies to this repo
#288 got four Copilot review comments. Three are lotek-side (`src/app/api_v1.py`,
`src/app/api_schemas.py`, `src/app/openapi_gen.py` — stale docstring references and an OpenAPI `security`
nit) and are out of scope here. The fourth lands squarely on the code being ported:

> `extensions/scribble/scribble/api_pat.py:568` — For JSON/base64 uploads, the size cap is enforced only
> *after* `base64.b64decode`, so a very large base64 string (up to `MAX_CONTENT_LENGTH`) will still be
> fully buffered/decoded in memory before returning 413. Add a preflight length check on the base64 string
> to reject oversized payloads before decoding.

**Already fixed upstream before #288 merged** — the merged `api_pat.py` carries the preflight (plus an
`isinstance(content_b64, str)` guard), so this branch inherits the fix rather than authoring it. What this
branch adds is the **test** for it, which #288's own suite covered only at the post-decode cap.

Checked the bound is exact rather than approximate: `max_b64_len = ((MAX + 2) // 3) * 4 + 4`, and a legal
25 MiB payload encodes to `ceil(MAX/3) * 4` = 34,952,536 chars against a bound of 34,952,540 — so no legal
upload is falsely rejected. The `# allow padding/newlines` comment slightly oversells it (a newline-bearing
payload is rejected by `b64decode(validate=True)` anyway), but the behaviour is right: nothing that would
have decoded within the cap gets a 413. The post-decode check remains the authoritative one.

### Provenance
Source is lotek's **merged `origin/main`** (`extensions/scribble/scribble/…`). #288 merged at
2026-08-13T19:20Z and its pre-merge worktree was deleted mid-session, so the merged tree is both the
available source and the better one (conflict-resolved).

Verified before copying that upstream's `api_pat.py` is a strict superset of this repo's: the only line
this repo has that upstream lacks is the `from scribble.deps import …` line, which upstream merely extends
with `get_config`. This repo's tenancy work (#11/#12/#13/#17) did not touch `api_pat.py` — #17 changed
`authz.py`/`blueprint.py`/`engagement_ui.py` — so a wholesale copy loses nothing.

### How the preflight test proves ORDERING, not just a status code
A test that simply posts something oversized would pass with or without the preflight (both paths end in
413/400 somewhere). So the test sends a payload that is **both oversized and invalid base64**: with the
preflight it is 413, and decode-first would be 400 (`invalid base64 content`). Verified by deleting the
preflight block, watching the test fail with exactly that 400, and restoring it.

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
