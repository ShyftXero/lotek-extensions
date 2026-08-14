# Scribble report redesign — vision & roadmap

Status: living design doc. Phase 1 is being built on `feat/scribble-report-redesign`; Phases 2–6 are
proposed, not yet scheduled. Owner: Eli McRae.

## The idea

The Scribble report should be **an authoring surface that renders a client-ready deliverable**, not a
printed dashboard. Concretely, a report is a **template of ordered blocks** of two kinds:

- **Authored narrative** — rich text the operator writes (Executive Summary, Overview, Scope notes,
  Approach/Methodology, Conclusion, Recommendations). Empty by default with a helpful placeholder and a
  generated first-draft; the operator edits in place.
- **Data widgets** — rendered from data (severity distribution bar, findings-at-a-glance index, finding
  cards, evidence galleries, methodology/coverage checklists, tooling & commands appendix).

The shipped layout is the **default template**; a future template editor lets operators reorder, hide,
and add blocks (dogfooding our own templating). Narrative blocks reuse Scribble's existing rich-text
primitive; data widgets and `{{variables}}` can pull from Scribble and — through the host seam — from
lotek core.

Reference primitives already in the tree:
- Rich text: ProseMirror JSON in `EngagementFinding.content_json`, one doc per named block; sanitized
  HTML render via `scribble/content/render_html.render_block`.
- Autosave: `POST/GET /findings/<id>/blocks/<block>` (`scribble/autosave_api.py`), debounced client
  `scribble/static/editor.js`, `content_html` cache.
- Templating: `{{TOKEN}}` resolution + lint + preview (`scribble/templating/`, `templating_api.py`),
  `build_full_context` / `make_var_resolver` / `known_variable_keys`.
- Report build: `scribble/reporting/context.py` → `ReportContext` → `render_html.py` / `render_docx.py`.
- Host seam: `scribble` reaches lotek only through the injected host contract (never imports lotek).

## Phases

### Phase 1 — Visual redesign (in progress, `feat/scribble-report-redesign`)
Light-first, print-native restyle of `render_html.py`: flat masthead + sticky nav, severity
distribution bar, findings-at-a-glance index, calm hairline finding cards, tinted severity tags,
document typography. CSS + additive widgets only; existing data + markup hooks preserved. See
`plans/feat-scribble-report-redesign.md`.

### Phase 2 — Operator-authored narrative sections
Lift the finding rich-text pattern to the **engagement** level so operators write the narrative.
- **Model:** `Engagement.content_json` (dict keyed `executive_summary`, `overview`, `scope_notes`,
  `approach`, `conclusion`, `recommendations`) + `content_html` cache; additive migration.
- **Autosave:** `POST/GET /engagements/<id>/sections/<name>` mirroring `autosave_api.py`; reuse
  `editor.js`.
- **Context/Render:** `ReportContext.sections: dict[str, str]`; `render_html` renders authored sections;
  the generated `narrative` (and generated scope/recommendations) become the **empty-state default** so a
  report with no authoring still reads well.
- **UI:** section editors on the engagement board; Edit ⇄ Preview toggle; empty-state placeholders.

### Phase 3 — Rich scope
Beyond `scope_type` (one word today): in-scope / out-of-scope targets, ranges, exclusions, window,
authorization ref, testing position. Either engagement-scoped **variables** (fits templating) or new
`Engagement` columns; render a proper Scope & Rules-of-Engagement block. Prefer variables first to avoid
schema churn; promote to columns only if they need to be first-class/queryable.

### Phase 4 — Tooling & Commands appendix (real data)
Show the scan modules and their **exact commands** as a reference appendix (clamped/scrollable on
screen, full in PDF). The command isn't in Scribble's model today — it lives in lotek's job metadata
(`metadata.pipeline[].command`). Plumb it across the **host seam** at promote/link time (e.g. a
`methodology`/`tooling` payload carried on the DTO or fetched via a host-contract call), stored on the
engagement and rendered as a widget. This is the original "unruly httpx command" — relocated to
reference, not the main narrative.

### Phase 5 — Layout template engine
Make the block order a first-class, editable **template**: reorder / hide / add blocks; per-block
config; our layout ships as the default template. Drag-to-reorder UI (pointer-based, touch-friendly —
prototyped in the mockup). Builds on the existing templating module.

### Phase 6 — Core-data binding ("pull any data from core")
Let templates surface arbitrary lotek core data (hosts, services, scan findings, job metadata) as
`{{variables}}` and data widgets, resolved **through the host contract** (never a direct import). Extends
`known_variable_keys` / the variable resolver with host-provided datasets; Phase 4 is the first concrete
instance of this.

## Principles
- Print-native: every on-screen affordance degrades to a clean PDF (chrome hidden, clamps removed,
  sections force-expanded).
- Keep the frozen-contract boundaries (`plans/CONTRACTS.md` ownership; `register(api_bp, bp)` wiring).
- v2-native: UUIDv7 PKs, cross-core refs via `sqlalchemy.Uuid`, authorization only through the host seam.
- Re-vendor into lotek after each phase lands; never hand-edit the vendored copy.
