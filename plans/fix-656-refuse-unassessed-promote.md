# Plan: fix/656-refuse-unassessed-promote

- **Branch:** `fix/656-refuse-unassessed-promote`  (worktree: lotek `.claude/worktrees/issue-656/extensions`, off `main`)
- **PR:** not opened yet (DRAFT — depends on core #648)
- **Status:** 🟡 in progress

## Purpose
The scribble half of lotek #656 (scan-outcome honesty map #644): `scribble_promote_job` must not
silently promote a job the scan **could not run** into a client deliverable — its zero findings render
as "No issues identified" (absence of evidence as evidence of absence). This is the only consequence in
the whole honesty map a customer sees.

The **core half** (`JobDTO.assessed` / `unassessed_modules`, populated in `_job_dto` via
`job_assessment`) is **already implemented on lotek branch `fix/648`** — do NOT re-add it. This branch
consumes that contract.

## Done
- [x] `scribble_promote_job` (api_pat.py) refuses `assessed is False` without
      `acknowledge_inconclusive=true` → **409 `inconclusive_job`** listing `unassessed_modules`.
- [x] `assessed is None` (legacy/unmeasured) is **never** refused; `getattr(job,"assessed",None)` keeps
      the gate dormant against a core predating #648 (fails safe until the contract lands).
- [x] Coverage gap recorded to the engagement's audit trail (`ext:scribble:promote_coverage_gap`)
      whenever `unassessed_modules` is non-empty — acknowledged inconclusive OR partially-unassessed.
- [x] Response surfaces `unassessed_modules` + `coverage_acknowledged` only when there's a gap
      (fully-assessed promote response byte-unchanged → existing exact-match tests stay green).
- [x] `_truthy` helper (JSON `true` or query `"true"`).
- [x] Test harness parity: `StubFindings.add_job` + `get_job` carry `assessed`/`unassessed_modules`
      (default `None` → every existing test unchanged).
- [x] 5 new tests in `test_machine_promote_job.py`; red-then-green on the refuse guard verified
      (gate neutralized → refuse test 200≠409, reverted → green).

## Remaining
- [ ] Reviews (`/security-review` + `/adversarial-reviewer`) + ack markers, then DRAFT PR.
- [ ] After core #648 merges + re-pin: mounted test in `lotek/tests/test_scribble_extension.py`
      (assessed=False over a real PAT → 409) and lift to ready.
- [ ] **Follow-up (separate issue):** report-RENDERED coverage note (which modules didn't run, in the
      deliverable). Deferred here — needs an engagement schema sink + an Alembic migration, and adding a
      migration to this concurrently-migrating ext repo risks forking the head. Audit-trail + response
      cover persistence for now.
- [ ] `engagement_ui.py` refuse-path is **N/A today** (promote is PAT-only; no UI promote route until
      #630's adopt-UI lands) — wire the same gate into that route when it exists.

## Notes / gotchas
- Editing the extensions **submodule** from a lotek-rooted session ⇒ core's PR gate runs but drops its
  lotek-specific markers (`is_submodule` true); still owe `--ack-review` + `--ack-adversarial`.
- `409` (conflict, resolvable via acknowledge), not `400` — the request is well-formed; the job STATE
  precludes promotion.
- pyrefly on api_pat.py reports 26 pre-existing errors (none in the changed ranges 541-547 / 1367-1400);
  this diff adds zero.
