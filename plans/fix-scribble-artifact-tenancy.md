# Plan: fix/scribble-artifact-tenancy

- **Branch:** `fix/scribble-artifact-tenancy`  (worktree: `.claude/worktrees/e-tenancy`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress (implementation done, opening PR next)

## Purpose
Close scribble artifact cross-tenancy holes tracked as issues #51/#52/#53. Verified against
origin/main @1c00281: #53's three cookie routes are already gated by the blueprint-wide
`authz._gate` (ext#11) and proven by `test_every_scoped_route_denies_a_non_member` — this branch
adds the regression-lock tests but ships no code change for #53. #52 is the one real hole —
`create_artifact` wrote a caller-supplied `finding_id` straight through with no engagement
cross-check. #51: the machine list route + PAT upload's drop-to-None/echo already existed on main
(ext#40's partial merge) — what was actually still missing was the **cookie** list route and the
engagement-page review/exclude UI.

## Done
- [x] Plan drafted
- [x] STEP 1: create_artifact drop-to-None cross-check + strict `_finding_id_or_400` parse +
      finding_id/finding_id_dropped echo on both the 201 and the idempotent-replay 200 (#52 core)
- [x] STEP 2: `_artifact_ctxs`/`_finding_ctx` defence-in-depth engagement_id cross-check (#52 DiD)
- [x] STEP 3: cookie list route `GET /scribble/api/engagements/<id>/artifacts` (#51) — auto-covered
      by the blueprint-wide tenancy gate (`engagement_id` is a `_DIRECT_KEYS` view arg)
- [x] STEP 4: machine list route — already existed on main (`api_pat.scribble_list_artifacts`),
      confirmed generically classified/covered by `test_scribble_machine_tenancy.py`, no code change
- [x] STEP 5: engagement.html "Engagement evidence" panel — reuses `_gallery.html`'s item
      classes so `artifacts.js`'s delegated toggle/caption/delete handlers apply unmodified; CSS
      scoped under `.scribble-evidence-panel`
- [x] STEP 6: 6 new tests in test_artifacts.py; red-then-green transcripts captured for both new
      guards (STEP 1 cross-check, STEP 2 DiD filter) by temporarily reverting each and confirming
      the new test(s) failed, then restoring and confirming green
- [ ] STEP 7: PR + gate acks + release comment

## Test results
`uv run --extra dev pytest --override-ini="addopts=" -q tests/test_artifacts.py
tests/test_scribble_tenancy_gate.py tests/test_scribble_machine_tenancy.py
tests/test_machine_artifacts.py` → **135 passed, 1 failed** (170s). The 1 failure
(`test_scribble_machine_tenancy.py::test_every_machine_route_id_converter_is_BOUNDED`) is
PRE-EXISTING on origin/main @1c00281 (confirmed via `git archive origin/main` into a scratch
checkout, unrelated to this branch's files) — not caused by this branch, not fixed by it.
`ruff check` and `pyrefly check` clean on every changed file.

## Notes / gotchas
- Repo is ext-only, no lotek changes.
- Cross-repo gate caveat: lotek's rails_gate.py adjudicates this PR cross-repo and will demand
  --ack-invariants which ext cannot earn; expect RAILS_OVERRIDE=1 on `gh pr create`, logged.
- Do not touch other sessions' worktrees under .claude/worktrees/.
- PAT route api_pat.scribble_upload_artifact already has the drop-to-None check (~line 1060) —
  mirror it exactly, no echoes/400s (parity with main, not the unmerged #40 branch).
