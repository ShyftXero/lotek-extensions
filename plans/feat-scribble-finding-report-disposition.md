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

## Baseline (measured on unchanged `main`, throwaway probe, then deleted)

Engagement: one `high`/`new`, one `critical`/`fixed`, one `critical`/`false_positive`.

| | before | after |
|---|---|---|
| `rollup.overall` | `critical` | `high` |
| `rollup.total` | 3 | 1 |
| `counts.critical` | 2 | 0 |
| `disposition_counts` | (field did not exist) | live 1 · remediated 1 · accepted 0 · excluded 1 |
| narrative | "identified **3 findings** … most significant exposures were Domain admin over SMB; **Phantom RCE**; SMB signing not required" | "identified **1 finding** … SMB signing not required" |
| `FindingCtx.status` | absent | raw + `disposition` + `status_label` |

The narrative line is the sharpest part of the before: the executive summary named a **false positive**
as a most-significant exposure, and a remediated finding alongside it.

## Done

- [x] Plan + evals committed first
- [x] Baseline measured and recorded (above) before any implementation
- [x] `report_disposition()` / `finding_status_label()` / `counts_toward_risk()` + the disposition
      constants in `scribble/enums.py` — the ONE home, beside `risk_rating`
- [x] `FindingCtx.status` / `.disposition` / `.status_label`; `SeverityRollup.disposition_counts`
      (all additive + defaulted)
- [x] `report_visible()` — inclusion = `include_in_report AND disposition != excluded`, computed once
- [x] `_tally` counts live only; `_tally_dispositions` counts all four; `_build_narrative` names live
      findings only
- [x] `render_html`: conditional `Status` column + finding-head badge + themed CSS (no severity palette)
- [x] `render_docx`: `status_label`/`disposition` scalars + a paragraph-level guarded Status line;
      `default.docx` regenerated via `build_default_docx.py`
- [x] E1/E2/E3/E5 in `tests/test_report_finding_disposition.py` (23 cases)
- [x] E4 drift guard in `tests/test_report_disposition_single_source.py`, with a planted positive
      control and per-EXPRESSION allowlist
- [x] `uvx ruff check scribble` clean; full scribble suite green

## Remaining

- [ ] PR opened with the before/after table in the body

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

## Corrections made while building (recorded, not hidden)

- **E3 was overstated.** "The HTML is byte-identical for an all-`new` engagement" is false: the report
  is one self-contained document, so its stylesheet is inlined whole and the `.status-badge` rules are
  present either way. What the test actually pins — and what matters — is that **no status markup is
  emitted**: no badge span, no `Status` column, no `ix-status` cell, and no Status line in the DOCX.
- **An empty "Ungrouped" section became possible.** `if ungrouped:` used to imply "has visible
  findings"; once `excluded` findings are filtered out, a bucket holding only false positives would
  render an empty heading. The synthetic bucket is now appended only when it has visible findings.
- **The DOCX needed a template change, not just a scalar.** The status line is authored in
  `build_default_docx.py` with **paragraph-level** `{% if %}` tags so a finding with no label leaves no
  blank line; `default.docx` is regenerated and committed.
- **The drift guard's allowlist is per EXPRESSION, not per file** — its first run correctly flagged
  `models.py`'s `default=FindingStatus.new` (a column default, not a derivation). Exempting the whole
  module would have exempted the next real derivation added to it.
