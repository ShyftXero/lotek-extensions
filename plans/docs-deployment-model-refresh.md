# Plan: docs/deployment-model-refresh

- **Branch:** `docs/deployment-model-refresh`  (worktree: `.claude/worktrees/scribble-report-redesign`, off `main`)
- **PR:** not opened yet
- **Status:** 🟢 ready to merge

## Purpose
Correct the one stale deployment doc. `CLAUDE.md` "Vendoring back into lotek" still told developers to run
the deleted `scripts/stage-extension.sh` and re-vendor a snapshot into lotek. The real model (since lotek
#292 / ext #16/#25) is **pinned git deps + `lotek.extensions` entry-points** — no vendoring. `README.md`
was already corrected on main ("How lotek consumes these"); this aligns `CLAUDE.md` with it.

## Done
- [x] Rewrote `CLAUDE.md` "Vendoring back into lotek" → "Shipping a change back to lotek (pinned git deps)"
      with the merge→auto-tag→re-pin workflow, matching `README.md`.

## Remaining
- [ ] Open PR into `main`; squash-merge.

## Notes
- `README.md` needed no change (already current). `scribble/lotek-extension.toml` header comments still
  mention `stage-extension.sh` (descriptive only); leave for a scribble-subdir PR rather than mixing repo
  files here.
