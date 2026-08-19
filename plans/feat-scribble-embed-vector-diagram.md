# Plan: feat/scribble-embed-vector-diagram

- **Branch:** `feat/scribble-embed-vector-diagram`  (worktree: `.claude/worktrees/r-diagram`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose
Embed a vector attack-path diagram into the scribble HTML report via an optional, backward-compatible
"Attack Paths" report block (ext#48). Scribble has no seam to reach vector directly at render time (a
separate extension, no host hook exposes it), so the minimal coherent slice stores a self-contained HTML
SNAPSHOT of vector's `export.html` on the scribble side (new `scribble_engagement_diagram` table) rather
than a live cross-extension fetch. A PAT machine endpoint lets an agent GET vector's export and POST it to
scribble end-to-end. The load-bearing guarantee is that a report with no linked diagram renders BYTE
IDENTICALLY to today.

## Done
- [x] `scribble/models.py`: new `EngagementDiagram` model (additive table, `Engagement.diagrams`
      relationship) — no alembic in scribble; `create_all` builds it automatically.
- [x] `scribble/reporting/context.py`: `DiagramCtx` dataclass + `ReportContext.diagrams` (default empty)
      + `_diagram_ctxs` populated in `build_report_context`.
- [x] `scribble/reporting/templates.py`: `diagrams` added to `BLOCK_KEYS` and inserted right after
      `findings` in every shipped template (`default`, `compliance`, `dark`).
- [x] `scribble/reporting/render_html.py`: `_render_diagrams`/`_render_diagram_item` (sandboxed
      `<iframe sandbox="allow-scripts">`, HTML-escaped `srcdoc`, NOT `allow-same-origin`), dispatched in
      `_render_block_by_key`, TOC entry, nav label, CSS. Returns `""` when `ctx.diagrams` is empty — the
      existing `_render_document` block-join already filters empty strings (it already did, for
      `evidence` — no join-logic change was needed here).
- [x] `scribble/api_schemas.py` + `scribble/api_pat.py`: `POST`/`GET`
      `/scribble/machine/engagements/<id>/attack-paths` (write/read scope, tenancy-checked before body
      read, idempotency-key support, size/length caps).
- [x] `tests/test_report_attack_path.py`: backward-compat (+ a real red-then-green transcript proving the
      empty short-circuit is load-bearing), with-diagram rendering (sandboxed iframe, escaping, ordering,
      excluded-diagram), and PAT round-trip/tenancy tests. 12 tests, all green.
- [x] `uvx ruff check` + `uv run pyrefly check` clean on all changed files.

## Remaining
- [ ] Full `uv run pytest -q` (whole scribble suite) — run and confirm no regressions before opening PR.
- [ ] `/security-review` + `/adversarial-reviewer` over `git diff main...HEAD`.
- [ ] Open DRAFT PR (Closes #48).

## Notes / gotchas
- **#36 rebase risk**: `feat/scribble-alembic-uuid-pks` (worktree `scribble-uuid-pks`, DO NOT TOUCH) is
  migrating scribble PKs to UUIDv7. `EngagementDiagram.engagement_id` is an `Integer` FK matching today's
  `scribble_engagements.id` type — whichever branch lands second owes a trivial FK-type rebase.
- Deferred (noted in PR body, not built here): docx image fallback (docx renderer is untouched and stays
  byte-identical), browser authoring UI, and a live (non-snapshot) vector resolver.
- Security: `srcdoc` is HTML-escaped via the existing `_esc` (uses `html.escape(quote=True)`), and the
  iframe sandbox omits `allow-same-origin` on purpose so the embedded snapshot's own JS cannot reach this
  report's DOM/cookies.
