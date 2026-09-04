# Plan: feat/scribble-strategic-recs-623

- **Branch:** `feat/scribble-strategic-recs-623`  (worktree: `.claude/worktrees/s623-strategic-recs`, stacked on `feat/scribble-retest-closeout-622`)
- **PR:** not opened yet (stacked chain link 5)
- **Status:** 🟢 ready to merge

## Purpose
Add authored **Strategic Recommendations** (lotek#623): an engagement-level, ordered list of
longer-horizon recommendations that renders as its own report section, alongside the tactical
per-finding remediation. Builds on the additive report-block pattern (#628 chains, #622 retest) and on
#620's `PATCH /engagements/<id>` write seam.

## Evals
- Deterministic graders (scribble suite): a NEW engagement (no recs) renders **byte-identically** to
  before this field existed (empty short-circuit, HTML + docx); a populated list renders one section
  with escaped items in order; the machine PATCH round-trips set/clear and rejects a non-list body; the
  GUI textarea (newline-split) persists the list. Baseline: field absent ⇒ the guard tests below go RED.

## Done
- [x] `models.py`: `Engagement.strategic_recommendations` (nullable JSON list) + one normalizer
      `normalize_strategic_recommendations` (single home; write + read seams all call it).
- [x] migration `e5a1c3d7b920_strategic_recommendations.py`: additive `ADD COLUMN` continuing the SINGLE
      head (`d3f5a7c9b1e2` attack-chains → `e5a1c3d7b920`); idempotent column-presence guard, matching the
      chain's house style.
- [x] `reporting/context.py`: `StrategicRecCtx(number, text)` + `ReportContext.strategic_recommendations`
      (additive, defaults empty), `_strategic_rec_ctxs` builder (normalized, numbered 1..N).
- [x] `reporting/render_html.py`: `_render_strategic_recommendations` (empty short-circuit →
      byte-identical when none), block dispatch, nav label, TOC entry.
- [x] `reporting/render_docx.py`: `_append_strategic_recommendations` mirror, appended right after the
      retest closeout so section order matches the HTML layouts.
- [x] `reporting/layouts.py`: new `strategic` BLOCK_KEY, placed after `retest` in both shipped layouts.
- [x] `api_pat.py`: `PATCH /engagements/<id>` extended to accept `strategic_recommendations` (list of
      strings, or `null`/`[]` to clear; non-list → 400); echoed in `_engagement_summary`.
- [x] `api_schemas.py`: `strategic_recommendations` on `PatchEngagementRequest`.
- [x] `engagement_ui.py` + `engagement_edit.html`: one textarea, newline-split, persisted via the shared
      normalizer.
- [x] Tests: `tests/test_strategic_recommendations.py` (model + render backward-compat + populated +
      red-then-green short-circuit + docx mirror) and cases in `test_machine_engagement_override.py`
      (PATCH set/clear/round-trip/non-list-400).

## Remaining
- [ ] Re-pin in lotek after merge (mounted `test_scribble_*`), per the extensions ship-back runbook.

## Notes / gotchas
- **DISPOSITION caveat (vs #622):** strategic recommendations are AUTHORED engagement-level prose, not
  findings — there is NO per-item report-disposition/inclusion signal to route through ext#166. The whole
  authored list IS the deliverable; the only inclusion switch is "the list is non-empty".
- Single alembic head confirmed via `alembic -c scribble/alembic.ini heads` (exactly one:
  `e5a1c3d7b920`). A fork = silent scribble mount failure (ext#169 precedent), so the
  `test_migration_single_head` guard covers it.
- The section carries **no figures**, so it does not disturb `context.number_figures`' report-wide figure
  sequence — safe to sit between `retest` and `methodology`/`evidence`.
- Column is nullable JSON; legacy/None rows coerce to `[]` at every read via the shared normalizer, so no
  cross-DB `server_default` cast is needed.
