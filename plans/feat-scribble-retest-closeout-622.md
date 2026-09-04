# Plan: feat/scribble-retest-closeout-622

- **Branch:** `feat/scribble-retest-closeout-622`  (worktree: `.claude/worktrees/s622-retest-closeout`, stacked on `feat/scribble-attack-chain-628`)
- **PR:** not opened yet (stacked chain link 4)
- **Status:** 🟢 ready to merge

## Purpose
Add the report **Retest Closeout** section (lotek#622): a finding → most-recent retest outcome table so a
deliverable shows the engagement's remediation state in one place. Builds on #621's `Retest` model and on
the additive report-block pattern (#628 chains, ext#48 diagrams).

## Done
- [x] `reporting/layouts.py`: new `retest` `BLOCK_KEY`, placed after `chains` in both shipped layouts.
- [x] `reporting/context.py`: `RetestCloseoutRow` + `ReportContext.retest_closeout` (additive, defaults
      empty), `_retest_closeout_rows` builder keyed to the ids that actually render at top level (no
      dangling anchors, per-host children folded into their parent), worst-severity-first.
- [x] `reporting/render_html.py`: `_render_retest_closeout` (empty short-circuit → byte-identical when no
      retest), block dispatch, nav label, TOC entry.
- [x] `reporting/render_docx.py`: `_append_retest_closeout` mirror, appended right after attack chains.
- [x] DISPOSITION: inclusion is ONE helper call (`_closeout_disposition_included`) that consumes ext#166's
      `report_disposition`/`DISPOSITION_EXCLUDED` when importable, else returns True with a
      `# TODO(ext#166)` at the single integration point. Not inline-re-derived anywhere.
- [x] Cases in `tests/test_report_print_media.py`: backward-compat (HTML + docx), populated table + label
      (not raw enum) + non-dangling anchor, red-then-green short-circuit, the ext#166 seam (patched in),
      docx mirror. All green; ruff + pyrefly clean; full scribble suite exit 0.

## Remaining
- [ ] When ext#166 (`feat/scribble-finding-report-disposition`) merges, the fallback in
      `_closeout_disposition_included` drops — the import becomes the whole decision (grep `TODO(ext#166)`).

## Notes / gotchas
- The closeout has **no figures**, so it does not disturb `context.number_figures`' report-wide figure
  sequence — safe to sit between `chains` and `methodology`/`evidence`.
- Latest round = `finding.retests[-1]` (the relationship is `order_by=Retest.created_at`).
- Only findings that render at **top level** get a closeout row, so every `#finding-<id>` link resolves.
- NO migration — the `Retest` table already ships from #621 (this branch's base).
