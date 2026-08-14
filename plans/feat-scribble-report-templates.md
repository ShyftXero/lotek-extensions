# Plan: feat/scribble-report-templates

- **Branch:** `feat/scribble-report-templates`  (worktree: `.claude/worktrees/scribble-report-redesign`, off `main` @ 319839e)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose
Prove the report **layout-template system** (Phase 5 of `plans/scribble-report-vision.md`) and enrich
finding cards. Two things in one render-layer branch (no DB migration, no new routes beyond a
`?template=` query param):

1. **Template engine** — refactor `render_html.py` so the document is assembled from an ordered list of
   named blocks defined by a selected **template**, and ship 3 templates to prove it:
   - `default` — current light-first layout (summary → findings → methodology).
   - `compliance` — a genuinely different order (summary → methodology → findings) to prove reorder/select.
   - `dark` — same blocks, dark theme forced (`<html data-theme="dark">`) to prove theming.
   A small in-report template switcher (`no-print`) + `render_report_html(ctx, template=...)` +
   `?template=` on the report route.
2. **Richer finding cards** — every finding always carries **Affected Assets** (aggregated from
   `target_host`/`target_port`/`target_url` + child-host instances + `AFFECTED` variable) and
   **Recommendations** (the `remediation` block, always labeled, empty-state prompt when unauthored).

## Constraints
- `default` template == current output byte-for-byte-ish so `tests/test_report_html.py` stays green
  (narrative before findings, group order, `risk-<overall>`, `finding sev-*`, children hooks, etc.).
- Theme "auto" (no stamp) for default/compliance = current behavior; only `dark` stamps `data-theme`.
- Ship path is re-pin, not vendor (see [[extension-deployment-model]] / plans/scribble-report-vision.md).

## Done
- [ ] Plan committed first.

## Remaining
- [ ] `reporting/templates.py`: `ReportTemplate(name,label,theme,blocks)` + registry + `get_template`.
- [ ] Refactor `render_html._render_document` to iterate a template's blocks; stamp theme on `<html>`.
- [ ] Block renderers: summary / findings(+filter bar) / methodology; keep anchors for the nav.
- [ ] Template switcher in the sticky bar; `?template=` on `/engagements/<id>/report`(+export).
- [ ] Finding card: always-on Affected Assets + Recommendations (new `_render_assets` + remediation block).
- [ ] `tests/test_report_templates.py` (order per template, dark theme stamp, unknown→default) +
      finding assets/recommendations assertions; keep `test_report_html.py` green.
- [ ] `uvx ruff check scribble` clean; render test subset green.

## Notes / gotchas
- Scope rendered verbatim (not title-cased) — group-order contract (see Phase 1 fix).
- Phase 2 (operator-authored sections + editor.js autosave + `Engagement.content_json` migration) is
  the NEXT branch; templates here reference narrative blocks but authoring UI comes later.
