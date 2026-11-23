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

| eval | before | after (target) |
|---|---|---|
| `tests/test_e2e_webui.py` on hhr2 | 1 passed | 1 passed, with the shared-section assertions |
| mutation: `doomed` = every finding sharing a group with a doomed row | GREEN (r1 defect) | RED |
| pre-fix route (no section prune) vs the new empty-section assertion | — | RED |
| `tests/test_unadopt_job.py` (new both-arms section tests) | — | N passed |
| `tests/test_migration_single_head.py` | 1 passed | 1 passed |

## Done
- [x] Reproduced Plague's reading of the fixture: `group_id` is NULL on all six seeded rows.
- [x] Confirmed the schema cannot distinguish an adopt-created section from a human one today.

## Remaining
- [ ] `FindingGroup.machine_authored` + additive migration off head `c7d9f1b3e5a2`.
- [ ] Prune emptied auto sections in `unadopt_job_destroy`; never touch manual/holding sections.
- [ ] Shared-section e2e fixture + both-sides section assertions.
- [ ] Docstring: "the subset the board path reads".
- [ ] Red proofs, hhr2 run, `### TEST RESULTS @ <sha>` comment.

## Notes / gotchas
- New column is nullable/defaulted, so `db.py`'s legacy additive path backfills it on adoption DBs too;
  the Alembic revision is still required for the Alembic-owned path.
- The hhr2 host must not launch a browser here: e2e runs happen only on `hhr2:~/code/lotek-ext-wt/402`.
