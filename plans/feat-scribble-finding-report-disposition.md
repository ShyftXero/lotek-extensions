# Plan: feat/scribble-finding-report-disposition

- **Branch:** `feat/scribble-finding-report-disposition`  (worktree: `.claude/worktrees/finding-disposition`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose

Build the decision resolved on lotek#618 (wayfinder map lotek#616): **finding `status` reaches the
report, and it drives ONE derived `report_disposition()` predicate.**

The bug this fixes is not "a badge is missing". Inclusion in a report is decided by
`include_in_report` **alone** (`reporting/context.py:166`), and `_tally()` counts **every** included
finding's severity into `SeverityRollup` → `risk_rating()` → the risk banner **and** the generated
narrative ("identified N findings"). So a finding the operator marked `false_positive` or `fixed`
**inflates the client's overall risk rating**. lotek#617 makes promote start mapping `DTO.status`, so
scanner-supplied statuses begin arriving and the wrong number gets worse.

## Evals (declared BEFORE the code — `edd`)

Graders, deterministic. The deliverable is the before/after on the same scope, not "the suite is green".

| # | Eval | Baseline (unchanged `main`) | Target |
|---|---|---|---|
| E1 | Rollup/risk/narrative for an engagement holding one `high` `new`, one `critical` `fixed`, one `critical` `false_positive` | overall = `critical`, total = 3 | overall = `high`, total = 1, `disposition_counts` = {remediated:1, excluded:1} |
| E2 | Cross-product: 6 `FindingStatus` × `include_in_report` {T,F} × {HTML, DOCX, rollup} | no surface shows status at all | one table of (rendered? counted? label?) agreed by **every** surface |
| E3 | Byte-identity: engagement where every finding is `new` | — | HTML+DOCX identical pre/post; no badge, no `Status` column, no disposition line |
| E4 | Drift guard: a second computation of the disposition outside `enums.py` | — | AST sweep fails on a new `FindingStatus.`/`status ==` site, reasoned allowlist |
| E5 | Labels never assert unrecorded conduct | — | `fixed`→"Remediated" (not "Fixed (verified)"), `accepted_risk`→"Risk accepted"; `tests/test_report_standing_prose.py` still green |

Each guard gets neutralised and watched go red (E1/E2 are red on `main` by construction — they *are*
the defect).

## Done

- [x] Plan + evals committed first

## Remaining

- [ ] E1/E2 tests written and confirmed RED on unchanged code (the baseline)
- [ ] `report_disposition()` + label map in `scribble/enums.py` (the ONE home, beside `risk_rating`)
- [ ] `FindingCtx.status` / `.disposition` / `.status_label`; `SeverityRollup.disposition_counts`
- [ ] inclusion + `_tally` gated on disposition in `build_report_context`; `_build_narrative` on the live total
- [ ] `render_html`: conditional `Status` column in `_findings_index` + finding-head badge
- [ ] `render_docx`: the equivalent
- [ ] E3 byte-identity pin, E4 drift guard, E5 label assertions
- [ ] ruff + the scribble suite green; PR body carries the E1–E5 before/after

## Notes / gotchas

- **`ReportContext` is a frozen contract**: every new field is **additive + defaulted**, nothing
  reordered or renamed (see the `artifacts` / `diagrams` / `activity_log` precedents in `context.py`).
- **Reuse the existing precedent, do not invent one**: `ChecklistItemCtx` already carries
  `status` (raw) + `bucket` (derived) + `bucket_label` (display), derived by the single
  `checklists.status_bucket()`. `FindingCtx` mirrors that shape.
- `include_in_report` stays the operator's explicit **veto**; `disposition` is the derived half.
  Inclusion = `include_in_report AND disposition != excluded`, computed **once** in
  `build_report_context` — never re-derived at a call site (repo directive: one derived predicate, one
  home).
- Disposition map: `live` = new/triaged/needs_retest (renders, counts) · `remediated` = fixed ·
  `accepted` = accepted_risk (both render, leave the ladder) · `excluded` = false_positive (gone).
- The board-side UX for an `excluded` finding is **out of this branch** — it is lotek#633.
- `confidence` is the sibling field and is **not** in scope: lotek#634 decides it.
