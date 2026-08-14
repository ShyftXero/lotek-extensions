# Plan: feat/scribble-report-redesign

- **Branch:** `feat/scribble-report-redesign`  (worktree: `.claude/worktrees/scribble-report-redesign`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose
The Scribble HTML report reads "Web 2.0" — glossy gradient header, saturated pill-spam, heavy
shadows, uppercase-everything, loose card stacking that scrolls forever. This branch ships **Phase 1**
of a report redesign: a modern, light-first, print-native deliverable that reads like a written report
rather than a printed dashboard. Phases 2–6 (operator-authored narrative, rich scope, tooling/command
appendix, layout templates, core-data binding) are scoped in `plans/scribble-report-vision.md` and are
NOT in this branch.

Design mockup (approved direction): a self-contained HTML artifact iterated with the client.

## Scope of THIS branch (Phase 1 — visual only, existing data only)
All changes are CSS + additive markup in `scribble/scribble/reporting/render_html.py`. No model,
migration, route, or `ReportContext` field changes. Every widget added is fed by data the context
already carries (`rollup`, `groups`, `scope_type`, `start/end_date`, `variables`). All existing markup
hooks the tests pin are preserved (`risk-<overall>`, `summary-narrative`, `finding sev-*`,
`id="finding-N"`, `children`/`children-table`, `sev-*`, `data:` inline, "No findings recorded").

## Done
- [x] Cut branch + plan file (this file), committed first.

## Remaining
- [ ] Replace `_CSS` with the light-first token system (both light + dark, print block).
- [ ] `_render_header`: sticky command bar (section jumps + print/expand/collapse) + flat masthead
      (no gradient), keeping `no-print` on chrome.
- [ ] `_render_summary`: add a **severity distribution bar** (from `rollup.counts`) and a
      **findings-at-a-glance index table** (severity · title→`#finding-N` · host · CVSS), plus a compact
      engagement/scope KV — while KEEPING the `risk risk-<overall>` banner and `summary-narrative`.
- [ ] Add stable section `id`s so the sticky nav can jump (summary, findings groups, checklists).
- [ ] Restyle findings / children / evidence / checklists via CSS only (markup hooks unchanged).
- [ ] `uvx ruff check scribble` clean; `cd scribble && python -m pytest -q` green
      (esp. `tests/test_report_html.py`, `test_e2e_flow.py`, `test_declared_variables.py`).
- [ ] Update `tests/test_report_html.py` ONLY where markup deliberately changed, with intent noted.
- [ ] Re-vendor note: after merge, re-run `scripts/stage-extension.sh` into lotek (do not hand-edit the
      vendored copy).

## Notes / gotchas
- `test_render_facts_polish.py` asserts against the **DOCX** renderer, not HTML — untouched here.
- `test_report_html.py` pins HTML markup strings; keep the hooks above or update the test intentionally.
- The severity bar is the one chart — semantic severity colors, not the accent; tabular-nums counts.
- Light-first was chosen (deliverables print to PDF); dark theme kept for on-screen via tokens.
- The exact scan command (the httpx example that kicked this off) belongs in Phase 4's tooling appendix
  and needs command data plumbed from lotek core — NOT in this branch.
