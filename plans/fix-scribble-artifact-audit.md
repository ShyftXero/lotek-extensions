# Plan: fix/scribble-artifact-audit

- **Branch:** `fix/scribble-artifact-audit`  (worktree: `.claude/worktrees/e-audit`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose
Issue #63 (INV-AUDIT-03): neither scribble artifact write route (upload, toggle
include_in_report/caption) emits an audit row, while every other mutating route in
`api_pat.py` calls `_audit`. Add the two missing `_audit` calls inside the existing
`open_session()` blocks, declare scribble's verbs in a new `[audit]` manifest table, and
wire a capturing audit hook into the test stub host so the gap is provable.

## Done
- [x] worktree + plan
- [ ] _audit on upload route (with db.flush() for artifact.id)
- [ ] _audit on update route (before/after include_in_report + caption)
- [ ] [audit] table in lotek-extension.toml
- [ ] stub_host audit_calls capture seam
- [ ] red-then-green tests
- [ ] lint/type/test checks
- [ ] PR open

## Remaining
- [ ] see Done

## Notes / gotchas
- Companion issue #57 (reader half — core's REQUIRED_ACTIONS/audit_view dropdown) is fixed
  in a SEPARATE PR in the lotek core repo, not here.
- Cross-repo gate quirk: the lotek-rooted rails_gate will demand --ack-invariants which this
  ext repo cannot earn; expect RAILS_OVERRIDE=1 on commit/PR (documented pattern, see issue #35).
