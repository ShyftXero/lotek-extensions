# Plan: feat/scribble-finding-report-disposition

- **Branch:** `feat/scribble-finding-report-disposition`  (worktree: `.claude/worktrees/finding-disposition`, off `main`)
- **PR:** https://github.com/ShyftXero/lotek-extensions/pull/166
- **Status:** 🟢 ready to merge

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
      (it lives in `scribble/enums.py`; see the correction below — it did NOT start there, and the
      claim that it had one home was false when first written)
- [x] `_tally` counts live only; `_tally_dispositions` counts all four; `_build_narrative` names live
      findings only
- [x] `render_html`: conditional `Status` column + finding-head badge + themed CSS (no severity palette)
- [x] `render_docx`: `status_label`/`disposition` scalars + a paragraph-level guarded Status line;
      `default.docx` regenerated via `build_default_docx.py`
- [x] E1/E2/E3/E5 in `tests/test_report_finding_disposition.py` (23 cases)
- [x] E4 drift guard in `tests/test_report_disposition_single_source.py`, with a planted positive
      control and per-EXPRESSION allowlist
- [x] `uvx ruff check scribble` clean; full scribble suite green

## Integrating main (2026-09-04, after PR #167 + #168 landed mid-branch)

`origin/main` moved while this branch was open: #617's superset build (`dispositions.py`,
`source_facts`), #620's severity override, and a merge revision fixing the two alembic heads those two
PRs created. GitHub called this branch "clean/mergeable", which says only that the *text* merges.

- **First merge was one commit stale** and produced **983 errors**, every one
  `alembic … Multiple heads are present`. Not this branch's doing and not a broken `main` — the
  head-merge revision had landed minutes earlier and I had fetched before it. Re-merged; gone.
  (Recorded because I briefly reported `main` as broken on the strength of a parser of mine that
  cannot read a tuple `down_revision`. It reads one head.)
- **The drift guard then earned its place.** With main merged, the suite had exactly ONE failure:
  `test_no_second_computation_of_the_report_disposition` flagging `dispositions.py:149`
  (`FindingStatus.new`). It was not a false positive — `status_from_dto` had re-implemented
  "unknown status → safe default", the same rule `enums._as_status` implements for the report side.
  Two copies of that rule mean promote can store one thing while the report interprets another, with
  nothing raising. Fixed by making the coercion public (`enums.coerce_finding_status`) and having
  `status_from_dto` delegate to it — so the guard now passes with **no allowlist entry**, which is the
  outcome worth having.
- **`CONTEXT.md` gained the disambiguation** these two changes made necessary: **Report Disposition**
  (a finding's fate in the deliverable — this branch) vs **Field Disposition** (a DTO field's home,
  origin and operator axes — #617). Same word, unrelated concepts, same package, landed a day apart.
- **#620's override composes with this cleanly**: it renders an operator-chosen headline band and
  leaves `rollup.overall` as the honest computed one. This branch makes that computed band honest-er
  (live findings only), which if anything reduces the need to override.

Targeted re-run after the fix: **48 tests, 0 failures** across the guard, this branch's disposition
tests, and main's `test_finding_dto_disposition_drift` / `test_source_facts_promote` /
`test_report_severity_override`.

## Remaining

- [x] Full suite green on the merged branch, then push — **1472 tests, 0 failures, 0 errors, 11 skipped**
      (junit XML, 2026-09-05, after the inclusion-fork fix below)
- [x] PR body/comment updated with the merged-main counts + the `report_visible` correction

## Notes / gotchas

- **`ReportContext` is a frozen contract**: every new field is **additive + defaulted**, nothing
  reordered or renamed (see the `artifacts` / `diagrams` / `activity_log` precedents in `context.py`).
- **Reuse the existing precedent, do not invent one**: `ChecklistItemCtx` already carries
  `status` (raw) + `bucket` (derived) + `bucket_label` (display), derived by the single
  `checklists.status_bucket()`. `FindingCtx` mirrors that shape.
- `include_in_report` stays the operator's explicit **veto**; `disposition` is the derived half.
  Inclusion = `include_in_report AND disposition != excluded`, computed **once** in
  `enums.report_visible` — never re-derived at a call site (repo directive: one derived predicate, one
  home). It sits in `enums.py` and not in `reporting/context.py` because of the import direction:
  `context` imports `findings_service` for the nesting rule, so `findings_service` cannot import back
  without a cycle; `enums` is the shared vocabulary both already import. Enforced by rule 3 of
  `tests/test_report_disposition_single_source.py`, not by convention.
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
- **The "one home" claim was FALSE when it was written, and an adversarial re-check caught it.**
  `report_visible()` landed in `reporting/context.py` with a docstring saying inclusion was "decided
  HERE, once, and nowhere else". The second home was
  `findings_service.rendered_top_level_count()` — which still filtered on `include_in_report` **alone**
  while its own docstring claimed to mirror `build_report_context`'s bucket walk "rather than
  re-deriving the rule". `api_pat` publishes that number as `top_level_count`, the field the machine-API
  docs tell an agent to quote as what the client sees. Reproduced: an engagement with one live `high`
  and one `critical` marked `false_positive` (both ticked for the report) **renders 1 card and reported
  2** — the same "wrong number in a client deliverable" defect this branch exists to fix, relocated to a
  sibling consumer.
  - The existing parity test `test_top_level_count_matches_what_the_renderer_produces` asserts exactly
    that equality and **stayed green over the bug**, because its board carried no statuses: the second
    half of the rule was unobservable. Fixed first, watched fail (`assert 7 == 5`), then fixed the code.
  - Two more finding-level filters were re-derived and are now the predicate: the `ungrouped` bucket
    pre-filter in `build_report_context`, and `_build_activity_log`, which would have listed a false
    positive's title in the activity appendix of a deliverable it is otherwise absent from.
  - `_tally_dispositions` keeps its bare `include_in_report` read on purpose (allowlisted with a
    reason): "how do the operator's kept findings split across dispositions" needs the veto WITHOUT the
    disposition half, or the `excluded` count would be structurally zero.
  - **What enforces it now:** rule 3 of the drift guard sweeps the package AST for any read of
    `.include_in_report` (attribute or `getattr`) outside a reasoned allowlist keyed by
    (file, enclosing function, expression). Planting the original bug back makes it fail with
    `findings_service.py:172 in rendered_top_level_count(): f.include_in_report`.
- **The drift guard's allowlist is per EXPRESSION, not per file** — its first run correctly flagged
  `models.py`'s `default=FindingStatus.new` (a column default, not a derivation). Exempting the whole
  module would have exempted the next real derivation added to it.

## Merge of `origin/main` @ `be78c99` (2026-09-05)

Two conflicts, both the `from scribble.enums import …` block, resolved as unions — main's enums
additions (`RetestOutcome` #621, `ReportFormat.json/csv` #627) are disjoint from the disposition block,
and nothing was moved or redefined on both sides.

- **`_closeout_disposition_included` deleted, not merged.** #622 landed it in `reporting/context.py` as
  a private half of `report_visible` — a `try: from scribble.enums import DISPOSITION_EXCLUDED,
  report_disposition / except ImportError:` probe for THIS branch, with a `TODO(ext#166)` to collapse it
  on merge. `_retest_closeout_rows` now calls `enums.report_visible`, the same predicate that built the
  `rendered_ids` it walks.
- **Drift-guard allowlist grew by 4, each verified non-finding-level.** `_chain_ctxs`
  `c.include_in_report` (#628 — `AttackChain` has no `status` column, so no disposition to AND in), and
  `findings_service`'s `FindingStatus.fixed/needs_retest/accepted_risk` from `_RETEST_OUTCOME_STATUS`
  (#621), which is the WRITE direction: what a status BECOMES, not what one MEANS. It cannot disagree
  with `report_disposition`, which interprets whatever it writes. Same class as the ORM column default.
- **Re-asserted by reading the merged tree:** the finding-level filters are `_order_findings`,
  `_build_activity_log`, `_retest_closeout_rows` and both buckets of `rendered_top_level_count` — all
  four call `report_visible`. main's new exporters (`render_json`/`render_csv`, #627) consume
  `ctx.groups`, so they inherit it rather than re-filtering.
- **No retest outcome can reach the `excluded` disposition** (`remediated`→`fixed`→`remediated`,
  `partially/not_remediated`→`needs_retest`→`live`, `accepted_risk`→`accepted`, `not_tested`→unchanged),
  so a retest can never silently drop a finding out of the deliverable.
- **#622's `test_closeout_consumes_report_disposition_when_it_is_importable` rewritten**, since its
  premise ("still unmerged") is now false and its monkeypatch made EVERY finding read as excluded. It
  drives a real `FindingStatus.false_positive` instead. Mutation-checked: reverting EITHER
  `report_visible` call to the `include_in_report` shape leaves it green — the two are redundant —
  reverting BOTH turns it red. It pins the behaviour; the AST drift guard pins the call sites.

### 🔴 The suite result is only meaningful over a SCRATCH alembic merge head

`origin/main` @ `be78c99` has forked into **two** heads on its own — `a7d2c4e6f810` (#171 refs/metadata)
and `e5a1c3d7b920` (#621→#628→#623) — both cut off `a7f3b9c1d2e4`. This branch adds **no** migration.
Verified directly against main's files: `ScriptDirectory(...).get_heads()` on a `git archive` of
`be78c99` returns both. The conftest does `upgrade head`, which alembic refuses on a multi-head chain,
so the run over the merge as committed was **1534 tests / 11 failures / 1047 errors — every single one
`CommandError: Multiple heads are present`, zero from any other cause**, i.e. no signal at all.

Re-run with a throwaway merge revision (`down_revision = ("a7d2c4e6f810", "e5a1c3d7b920")`, **not
committed**, deleted after): **1534 tests, 0 failures, 0 errors, 11 skipped** (698s). That is the honest
result for this branch. `tests/test_migration_single_head.py` stays RED until whoever forked main rejoins
it — fixing someone else's migration fork does not belong on this branch.

## Merge of `origin/main` @ `c1306c8` — the honest full-suite run (2026-09-05)

The caveat above is discharged. `main` rejoined its own alembic fork in ext#186
(`b8e4d2f6a130`, `down_revision = ("a7d2c4e6f810", "e5a1c3d7b920")`), so the conftest's
`upgrade head` works again and the suite gives real signal for the first time on this branch.

- **Merge was clean** — no conflicts, two additive files from `main`
  (`plans/fix-scribble-refork-alembic-heads.md`, the merge revision). Nothing in this branch's
  diff moved.
- **Full suite over the merge as committed, no scratch revision:**
  **1534 tests · 0 failures · 0 errors · 11 skipped** (701s, junit XML, `-p no:randomly`).
  Same 1534 as the scratch-head run, which is the expected equality: ext#186's revision is a
  no-op merge node, so it changes what alembic will *do*, not what any test asserts.
- **`test_migration_single_head::test_migration_chain_has_a_single_head` PASSES.** It was the
  branch's one standing RED and it was never this branch's to fix — the head it wanted rejoined
  belonged to `main`.
- `uvx ruff check scribble` clean; `pyrefly check` clean (0 errors) on all 13 changed `.py` files.
