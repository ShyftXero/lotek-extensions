# Plan: feat/scribble-report-redesign

- **Branch:** `feat/scribble-report-redesign`  (worktree: `.claude/worktrees/scribble-report-redesign`, off `main`)
- **PR:** https://github.com/ShyftXero/lotek-extensions/pull/28
- **Status:** 🟢 ready to merge

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
- [x] Replaced `_CSS` with the light-first token system (light + dark tokens, print block).
- [x] `_render_header`: sticky command bar (section jumps + expand/collapse/print) + flat masthead.
- [x] `_render_summary`: added `_sev_bar` (severity distribution) + `_findings_index` (severity ·
      title→`#finding-N` · host · CVSS) + metrics row; KEPT `risk risk-<overall>` banner and
      `summary-narrative`. Scope rendered verbatim (not title-cased) so the group-order contract holds.
- [x] Section anchors `#sec-findings` / `#sec-methodology` in `_render_document` for the sticky nav.
- [x] Findings / children / evidence / checklists restyled via CSS only (markup hooks unchanged).
- [x] `uvx ruff check scribble` clean.
- [x] Tests: `test_report_html.py` (8) + render/docx/variable/e2e-flow/vuln-db/checklist subset (55)
      all green. No test edits needed — every pinned markup hook preserved.
- [x] Eyeballed the actual render (sample context through the new code) — matches the approved mockup.

## Remaining
- [ ] Open PR into `main`; squash-merge.
- [ ] After merge: re-vendor into lotek via `scripts/stage-extension.sh` (never hand-edit the vendored
      copy). Then Phases 2–6 per `plans/scribble-report-vision.md`.

## Pre-existing failures (NOT caused by this branch, verified against base #26)
- `tests/test_skill.py` — the `scribble/skill/scribble-report-refine/` dir does not exist on base #26.
- `tests/test_scribble_machine_tenancy.py::test_every_engagement_scoped_machine_route_denies_a_foreign_client`
  — machine-API auth test; unrelated to rendering; fails identically with this branch's change stashed.

## Notes / gotchas
- `test_render_facts_polish.py` asserts against the **DOCX** renderer, not HTML — untouched here.
- `test_report_html.py` pins HTML markup strings; keep the hooks above or update the test intentionally.
- The severity bar is the one chart — semantic severity colors, not the accent; tabular-nums counts.
- Light-first was chosen (deliverables print to PDF); dark theme kept for on-screen via tokens.
- The exact scan command (the httpx example that kicked this off) belongs in Phase 4's tooling appendix
  and needs command data plumbed from lotek core — NOT in this branch.
