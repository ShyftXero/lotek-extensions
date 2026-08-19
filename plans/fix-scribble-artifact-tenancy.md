# Plan: fix/scribble-artifact-tenancy

- **Branch:** `fix/scribble-artifact-tenancy`  (worktree: `.claude/worktrees/e-tenancy`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose
Close scribble artifact cross-tenancy holes tracked as issues #51/#52/#53. Verified against
origin/main @1c00281: #53's three cookie routes are already gated by the blueprint-wide
`authz._gate` (ext#11) and proven by `test_every_scoped_route_denies_a_non_member`. #52 is the one
real hole — `create_artifact` writes a caller-supplied `finding_id` straight through with no
engagement cross-check (the PAT sibling already drops-to-None). #51's "silent publish" premise is
false on main (no engagement-level artifacts field in ReportContext, no Evidence appendix) — the
legitimate remaining ask is a missing engagement-artifacts list route (cookie + machine) and a
review/exclude UI panel.

## Done
- [x] Plan drafted

## Remaining
- [ ] STEP 1: create_artifact drop-to-None cross-check (#52 core)
- [ ] STEP 2: _finding_ctx defence-in-depth filter (#52 DiD)
- [ ] STEP 3: cookie list route GET /engagements/<id>/artifacts (#51)
- [ ] STEP 4: machine list route (#51)
- [ ] STEP 5: engagement.html UI panel (#51)
- [ ] STEP 6: tests, red-then-green transcripts
- [ ] STEP 7: PR + gate acks + release comment

## Notes / gotchas
- Repo is ext-only, no lotek changes.
- Cross-repo gate caveat: lotek's rails_gate.py adjudicates this PR cross-repo and will demand
  --ack-invariants which ext cannot earn; expect RAILS_OVERRIDE=1 on `gh pr create`, logged.
- Do not touch other sessions' worktrees under .claude/worktrees/.
- PAT route api_pat.scribble_upload_artifact already has the drop-to-None check (~line 1060) —
  mirror it exactly, no echoes/400s (parity with main, not the unmerged #40 branch).
