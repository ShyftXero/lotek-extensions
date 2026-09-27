# Plan: feat/lot-72-templated-prose

- **Branch:** `feat/lot-72-templated-prose`  (worktree: `.claude/worktrees/lot-72`, off `main`)
- **PR:** https://github.com/ShyftXero/lotek-extensions/pull/269
- **Status:** 🟢 ready to merge (pending re-review of the latest commit)

## Purpose
LOT-48/4: a no-LLM templated prose module (`scribble.reporting.narrative`) for the engagement report.
Two deterministic surfaces over structured `ReportContext` / `FindingCtx` facts, wired into
`ReportContext.narrative` and `FindingCtx.one_liner`: an executive summary keyed on the severity rollup,
the coverage posture, and the discovered cradle-to-DA attack-path count; and a per-finding one-liner. The
default report path emits every word with zero model calls. An optional, off-by-default AI-polish seam can
rephrase already-rendered prose for flow only, never in the decision path.

## Done
- [x] `narrative.py`: exec-summary + one-liner Jinja templates, deterministic helpers, off-by-default
      `polish()` seam (identity unless env flag AND host hook, fails closed to input).
- [x] `context.py`: `_build_narrative` delegates to the module; `one_liner` filled per finding; exec-summary
      facts (path count, coverage-limited-job count) built off structured data; `polish()` applied.
- [x] `test_report_narrative_no_llm.py`: pins the three ACs (zero model calls; never "no issues" for an
      unassessed job; AI flag off + provider-off byte-for-byte identical) plus template mechanics.
- [x] Ghostwriter's LOT-72 Lotek-voice copy wired in (commit 84d882f): exec summary renders 4 coverage
      branches (FOUND / RAN-CLEAN / COULD-NOT-RUN / MIXED); one-liner is severity + location + CVE + KEV.
- [x] narrative / standing-prose / prose suites green; ruff clean.

## Remaining
- [ ] Re-run `/security-review` + `/adversarial-reviewer` against HEAD 84d882f (prior markers were keyed to
      the earlier HEAD) before merge.
- [ ] LOT-74 (Ghostwriter): formal voice pass over the wired strings -- a check, not a rewrite.

## Notes / gotchas
- `one_liner` is computed onto `FindingCtx` but not yet consumed by any renderer -- only `ctx.narrative`
  (the exec summary) renders into HTML/DOCX today.
- Could-not-run branches render on the coverage-limited-job COUNT. Named-scope wording (`gap_reason`,
  `covered_scope`, `unassessed_scope`) is deferred until the coverage note surfaces module/scope strings --
  not fabricated (evidence-first).
- The coverage note is itself a report-visible info finding, so an engagement whose only artifact is a
  coverage note reads as MIXED (found + a gap), not COULD-NOT-RUN. Pre-existing rollup behavior.
- House rules on every string: no em dashes, no semicolons, no UTF middle dots (all three also fail
  `test_report_standing_prose`).
