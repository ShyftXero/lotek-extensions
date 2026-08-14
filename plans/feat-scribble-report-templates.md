# Plan: feat/scribble-report-templates

- **Branch:** `feat/scribble-report-templates`  (worktree: `.claude/worktrees/scribble-report-redesign`, off `main` @ 319839e)
- **PR:** not opened yet
- **Status:** 🟢 ready to merge

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
- [x] Plan committed first.
- [x] `reporting/templates.py`: `ReportTemplate` + registry (`default`/`compliance`/`dark`) + `get_template`/`list_templates`.
- [x] `render_html`: `_render_block_by_key` dispatch; `_render_document` iterates the template's blocks
      and stamps `<html data-theme>` (light/dark only).
- [x] Blocks: summary / findings(+filter bar) / methodology, anchors kept for the sticky nav.
- [x] Layout switcher in the sticky bar (`#template-select`, reloads with `?template=`); `?template=` wired
      on `/engagements/<id>/report` + `/report/export` (html + zip).
- [x] Finding card: always-on **Affected Assets** (`_affected_assets`/`_render_affected_assets`, aggregating
      target host/url + child hosts + `AFFECTED`) and **Recommendations** (`_render_recommendations` from the
      `remediation` block, empty-state prompt when unauthored). Dropped the free-floating target chips.
- [x] `tests/test_report_templates.py` (9) + `test_report_html.py` (8) green; wider render subset 47/47.
- [x] `uvx ruff check scribble` clean.
- [x] Fixed stale `stage-extension.sh` comments in `scribble/lotek-extension.toml` (deployment is pinned deps).

## Remaining
- [ ] Open PR into `main`; squash-merge → CI cuts a release tag.
- [ ] Bump the lotek pin to that tag (deploy) — the session goal.

## Notes / gotchas
- Scope rendered verbatim (not title-cased) — group-order contract (see Phase 1 fix).
- Phase 2 (operator-authored sections + editor.js autosave + `Engagement.content_json` migration) is
  the NEXT branch; templates here reference narrative blocks but authoring UI comes later.
