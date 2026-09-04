# Plan: docs/rails-release-gate-note

- **Branch:** `docs/rails-release-gate-note`  (worktree: `.claude/worktrees/rails-note`, off `main`)
- **PR:** not opened yet
- **Status:** 🟢 ready to merge

## Purpose

lotek core moved its hard gate from `gh pr create` to cutting a release (core PR
`chore/rails-release-gate`, 2026-09-04, on Eli's directive: *"the new gate for the project is cutting a
release. prs want tests but can be flexible."*). This repo's `CLAUDE.md` describes its own PR gate as
"the real one", which is still true **here** — and the reason it is still true is worth writing down,
because the obvious next move (mirror core, loosen this gate too) would be wrong.

## Done

- [x] `CLAUDE.md`: a section explaining why this repo's PR gate stays a gate — extensions have no
      release step of their own, so there is nothing to hand the requirement to. Core's release
      evidence is core's suite + core's invariant contract, and neither runs an extension's tests.
- [x] Recorded the cross-repo hook subtlety: the executing hook belongs to the *session's* project
      dir, and core's gate only drops its lotek-specific markers when `R.is_submodule(cwd)` is true —
      false for a standalone `lotek-extensions` clone, which is why an extensions PR opened from a
      lotek-rooted session legitimately needs `RAILS_OVERRIDE=1` until core's advisory change lands.

## Remaining

- [ ] Open the PR.

## Notes / gotchas

- **Docs-only branch**, so `--ack-tests` does not apply (this repo's gate exempts a branch whose every
  changed path is `.md`); both reviews still do.
- The alternative shape — mirror core and let extension tests be advisory at the PR — is deliberately
  *not* taken and the file says why. If it is ever wanted, the missing piece is a release-time gate
  that runs the extensions' own suites, not a matching loosening.
- No behaviour change here: `.claude/hooks/rails_gate.py` in this repo is untouched.
