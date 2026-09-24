# Plan: feat/scribble-report-finding-grouping

- **Branch:** `feat/scribble-report-finding-grouping` (off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose

Finish finding-grouping Phase 1b: the board got a By-vulnerability / By-host toggle; now the **report
deliverable** (`reporting/render_html.py`) gets the same grouping so the rendered/PDF client report
shows fleet-wide vulns collapsed too — the piece called out as remaining after the board shipped.

## Design (from the deliverable-render scout)

- The renderer is a PURE function run offline by ~20 tests, where the host seam returns `[]`. So grouping
  is computed **in `build_report_context`** (in-request in prod) and threaded onto `ReportContext` as
  additive `findings_by_kind` / `findings_by_host`, reusing the board's exact plumbing
  (`flatten_for_grouping` → `finding_grouping_adapter.board_finding_to_group_row` → `host.group_findings`).
  Never call the seam from the renderer.
- Fed the **report-visible** ORM findings only (excluded groups/findings dropped) — the deliverable shows
  fewer than the board, so excluded findings never leak into the client rollup.
- Rendered as a **static block** (`rollups`), not a JS toggle — the deliverable is the print/PDF target.
  Reuses the themed `.index` table styling (theme-aware + print-safe, no new CSS). Omit-when-empty →
  an unmounted/offline render is byte-identical.

## Done
- [x] `reporting/context.py`: additive `findings_by_kind`/`findings_by_host` on `ReportContext`; built in
      `build_report_context` over the report-visible findings (accumulated as each group is ordered).
- [x] `reporting/render_html.py`: `_render_rollups` static block (By vulnerability + By host tables),
      wired into `_render_block_by_key`, `_NAV_LABELS`, and `_toc_entries` (same condition as the render,
      so the TOC entry appears iff the section does).
- [x] `reporting/layouts.py`: `rollups` block key, placed immediately before `findings` in both layouts.
- [x] Tests: `tests/test_report_finding_grouping.py` — renderer emits/omits the section; context feeds the
      seam report-visible findings only (excluded finding never reaches the rollup). Existing `test_report_*`
      family stays green (empty block byte-identical).

## Remaining
- [ ] Reviews + acks → PR → merge → tag. Core re-pin + a mounted deliverable eval.
- [ ] (Not this branch) DOCX/CSV/JSON exports don't render the rollup — they ignore the new ctx fields.
      Follow-up if wanted; `render_docx` hardcodes its own section order (doesn't iterate `BLOCK_KEYS`).

## Notes / gotchas
- `build_report_context` is only ever called on a RELOADED board in prod; a test that builds + renders in
  one session can trip `_build_activity_log`'s mixed-tz sort — reload in a fresh session (as the other
  render tests do).
- Core companion branch: `feat/scribble-report-finding-grouping` (re-pin + mounted eval).
