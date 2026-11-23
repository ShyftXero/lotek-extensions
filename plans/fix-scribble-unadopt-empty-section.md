# Plan: fix/402-unadopt-destroy (follow-up to PR #406, Plague r1)

- **Branch:** `fix/402-unadopt-destroy`  (worktree: `/home/user/lotek-ext-wt/unadopt`, off `main`)
- **PR:** https://github.com/ShyftXero/lotek-extensions/pull/406
- **Status:** 🟡 in progress

## Purpose
Plague's r1 review of PR #406 raised one CONCERN with two halves, both from issue #402's contract:

1. the browser test's fixture does not reproduce the "two-job shared-section case" — every seeded row is
   `group_id=None`, so a "clear the whole shared section" regression stays green;
2. **product behaviour**: `unadopt_job_destroy` never touches `FindingGroup` and the board renders an
   empty group, so a destroy that empties an adopt-created section leaves a stale empty job block —
   while #402 says "an empty automatically-created section should not appear as a stale job block. Do
   not delete a manually authored section silently."

## Hypothesis (declared before the code)
- **H1** — a `FindingGroup` created by the adopt path (`findings_service._find_or_create_section`) is not
  distinguishable from a hand-authored one in the current schema (`name`, `assessment_type_id`,
  `order_index`, `order_mode`, `include_in_report`; a human's "Report section type" select sets
  `assessment_type_id` too). So provenance must be persisted: a `machine_authored` boolean, set ONLY on
  the adopt path's own create, reused-but-not-flipped when it find-or-creates.
- **H2** — with that column, a destructive un-adopt removes a section IFF it is `machine_authored` AND
  empty after the delete. Sections with surviving findings and hand-authored sections always survive.
- **H3** — a shared-section fixture (one `External` group holding both jobs' rows + both coverage notes
  + the manual finding) REDs under the "expand `doomed` to the whole group" mutation that stayed green
  in r1.

## Evals
Baseline (recorded, review r1 on hhr2): `1 passed, 7 deselected in 2.27s` for
`uv run pytest -p no:cacheprovider -q -o addopts="" tests/test_e2e_webui.py` @ `aaaa296`.

| eval | before | after (measured) |
|---|---|---|
| `tests/test_e2e_webui.py` on hhr2 | 1 passed, 7 deselected (r1) | **8 passed** (whole file), `1 passed, 7 deselected` targeted |
| mutation: `doomed` = every finding sharing a group with a doomed row | GREEN (r1 defect) | **RED** — `1 failed, 7 passed`, line 554 |
| pre-fix route (no section prune) vs the new empty-section assertion | — | **RED** — `1 failed, 7 passed`, line 633 |
| `tests/test_unadopt_job.py` + `tests/test_board.py` (local) | 76 passed | **80 passed** |
| `tests/test_migration_single_head.py` (+deps) single head | 1 passed | 37 passed, 5 skipped |

## Done
- [x] Reproduced Plague's reading of the fixture: `group_id` is NULL on all six seeded rows.
- [x] Confirmed the schema cannot distinguish an adopt-created section from a human one today.

- [x] `FindingGroup.machine_authored` + additive migration off head `c7d9f1b3e5a2`.
- [x] Prune emptied auto sections in `unadopt_job_destroy`; never touch manual/holding sections.
- [x] Shared-section e2e fixture + both-sides section assertions.
- [x] Docstring: "the subset the board path reads".
- [x] Red proofs (three unit arms + two e2e mutations), hhr2 run.

## Remaining
- [ ] `### TEST RESULTS @ <sha>` comment on PR #406.
- [ ] Re-review (Plague) → merge.

## Notes / gotchas
- New column is nullable/defaulted, so `db.py`'s legacy additive path backfills it on adoption DBs too;
  the Alembic revision is still required for the Alembic-owned path.
- The hhr2 host must not launch a browser here: e2e runs happen only on `hhr2:~/code/lotek-ext-wt/402`.

## Round 2 (Plague r2 @ 9293d7e, 2026-11-23)

Plague's r2 CONCERN: `machine_authored` survives a HUMAN configuring the section. Two measured harms:

- rename `External` → `Web Application Findings` via `POST /scribble/api/groups/<id>`, then destroy the only
  job → the renamed section is gone (a person named it; the destroy discards that);
- toggle `include_in_report=False` (a client-deliverable decision), destroy, re-adopt → `create_group`
  re-creates the section with `include_in_report=True`, so an excluded section silently returns IN the
  client report.

### Hypothesis (declared before the code)

- **H4** — provenance must be cleared by the HUMAN authoring route, not by a field-value heuristic:
  `update_group` (both surfaces) is that route — nothing in the adopt/auto-section machinery calls it — so
  any accepted edit there means a person configured the section. This is also the only place the signal
  can live: `order_mode` is flipped to `manual` by `place_finding` for a machine placement too, so a
  model-level or value-based predicate would mis-fire.
- **H5** — with provenance cleared, the section is not pruned, so re-adopt REUSES it
  (`_find_or_create_section` finds by assessment type) and the operator's `include_in_report=False`
  survives. That closes the report-exclusion harm without touching `create_group`.
- **H6** — the reviewer's alternative (name the doomed sections in preview/confirm) is insufficient on its
  own: it does not fix the re-adopt resurrection at all, and it makes an irreversible delete depend on the
  operator reading a dialog. Chosen: clear-on-any-human-edit.

### Evals (before = @ 9293d7e, measured)

| eval | before | after |
|---|---|---|
| `test_update_group_rename_clears_machine_authored` (UI route) | RED: still True | pass |
| `test_update_group_include_toggle_clears_machine_authored` (UI) | RED | pass |
| `test_update_group_order_mode_clears_machine_authored` (UI) | RED | pass |
| machine PATCH twin clears provenance (`test_machine_findings_crud.py`) | RED | pass |
| destroy after a rename KEEPS the renamed section (`test_unadopt_job.py`) | RED: section gone | pass |
| destroy after `include_in_report=False` + re-adopt keeps the exclusion | RED: re-created True | pass |
| migration: nullable-with-NULLs column → backfilled + NOT NULL | RED: stays nullable | pass |
| `removed_section_names` asserted (`["External"]` / `[]`) | unasserted | pass |
| `tests/test_unadopt_job.py tests/test_board.py` (local) | 80 passed | TBD |
| `tests/test_e2e_webui.py` (hhr2) | 8 passed | TBD |
