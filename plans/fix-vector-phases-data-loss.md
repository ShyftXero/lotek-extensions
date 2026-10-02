# Plan: fix/vector-phases-data-loss

- **Branch:** `fix/vector-phases-data-loss`  (worktree: `.claude/worktrees/fix-vector-250`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose
Fix ext#250: setting a non-intro phase's Phase # to 0 makes the change irreversible (the phase renders
as an "intro slide" with no Phase # field) and clobbers the real intro phase's content in the viewer.

## Root Cause
`vector-editor.js` line 207: `if (ph.intro || ph.n === 0)` — when a non-intro phase gets n=0, it
renders as the "intro slide" section (no Phase # field), trapping the user. Two phases with n=0 also
hit the phaseMap collision in the viewer (last one wins, clobbers intro content).

## Fix
1. `vector-editor.js`: change `if (ph.intro || ph.n === 0)` to `if (ph.intro)` — only phases with
   the `intro` flag render as intro slides.
2. `vector-editor.js`: add `min="1"` to the Phase # number input (fNum for phases.*.n) so the HTML
   constraint prevents typing 0 for non-intro phases.
3. `vector/schema.py`: in `_norm_phase`, enforce `lo=1` for non-intro phases so any n=0 saved on a
   non-intro phase is clamped to 1 on next normalize.

## Done
- [x] Root cause identified

## Remaining
- [ ] Fix editor JS (render condition + min attribute)
- [ ] Fix schema normalization (lo=1 for non-intro)
- [ ] Add test that fails before fix, passes after
- [ ] ruff + pyrefly clean
- [ ] --ack-tests, --ack-review, --ack-adversarial
- [ ] PR open

## Notes / gotchas
- The `lo=1` clamp in schema.py will auto-correct any existing saved diagram on next save — that's the
  desired behaviour (repair the data).
- Old intro phases always carry `intro: True` in both schema and seed data, so changing the JS
  condition is safe.
