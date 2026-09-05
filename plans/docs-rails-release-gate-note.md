# Plan: docs/rails-release-gate-note

- **Branch:** `docs/rails-release-gate-note`  (worktree: `.claude/worktrees/rails-note`, off `main`)
- **PR:** https://github.com/ShyftXero/lotek-extensions/pull/170
- **Status:** 🟢 ready to merge

## Purpose

lotek core is moving its hard gate from `gh pr create` to cutting a release
(**ShyftXero/lotek#638**, branch `chore/rails-release-gate`, opened 2026-09-04, on Eli's directive:
*"the new gate for the project is cutting a release. prs want tests but can be flexible."*) — **still
open as a draft as of 2026-09-05**. This repo's `CLAUDE.md` describes its own PR gate as
"the real one", which is still true **here** — and the reason it is still true is worth writing down,
because the obvious next move (mirror core, loosen this gate too) would be wrong.

## Done

- [x] `CLAUDE.md`: a section explaining why this repo's PR gate stays a gate — extensions have no
      release step of their own, so there is nothing to hand the requirement to. Core's release
      evidence is core's suite + core's invariant contract, and neither runs an extension's tests.
- [x] Recorded the cross-repo hook subtlety: the executing hook belongs to the *session's* project
      dir, so a lotek-rooted session opening an extensions PR gets *core's* gate. Core drops its
      lotek-specific markers for a submodule (`R.is_submodule(cwd)`, false for a standalone
      `lotek-extensions` clone) **and, independently, for any docs-only branch**.
- [x] Opened PR #170.
- [x] **Correction 1** (adversarial review, 2026-09-05): the section stated core's change in the
      completed past tense. It has not landed — #638 is OPEN/draft, core `main`'s
      `scripts/cut-release-tag.sh` contains no marker check, `docs/RAILS.md` has no §5c, and
      `_g_pre_pr_review` still returns a hard `deny` verdict (verified against the `origin/main`
      blobs, not a working tree). Rewritten to attribute the change to #638 by number
      and to read correctly whether or not it has merged; the self-contradicting "until core's advisory
      change lands" bullet is reconciled with the new phrasing. The section's *argument* is unchanged —
      only the factual premise about core's current state was wrong.
- [x] **Correction 2** (same review): the override rationale was factually wrong. It claimed core's
      gate "demands core's `--ack-invariants` — a marker no extension diff can earn". Core's
      `_g_pre_pr_review` calls `_branch_is_docs_only`, which drops BOTH `--ack-tests` and
      `--ack-invariants` for a branch whose every changed path is `.md`; this branch's audit trail in
      `.git/claude-rails-audit.jsonl` shows `pre-pr-review warn "docs-only branch: --ack-tests /
      --ack-invariants not required"` immediately before `override "bypassed via RAILS_OVERRIDE=1"`.
      What the override actually bypassed was `--ack-review` + `--ack-adversarial`, which are
      repo-agnostic and earnable here. CLAUDE.md now says so plainly and flags the earlier text as an
      overstatement rather than silently editing it.
- [x] **Earned the two markers** rather than leaving the override standing: reviewed the branch diff
      (2 markdown files, 67 added lines) for security and adversarially, then recorded `--ack-review`
      and `--ack-adversarial` against the final head.
- [x] PR #170 body updated with the corrected gate-notes paragraph and a "Corrections" section.

## Remaining

- Nothing.

## Notes / gotchas

- **Docs-only branch**, so `--ack-tests` does not apply (this repo's gate exempts a branch whose every
  changed path is `.md`); both reviews still do — and they were earned, not overridden.
- Security review of the diff: the change adds only prose to two `.md` files. No code, no hook, no
  workflow, no dependency, no credential, no URL beyond `ShyftXero/lotek#638` and the existing PR link.
  Nothing here is executed by anything; `.claude/hooks/rails_gate.py` is untouched (`git diff
  origin/main...HEAD --name-only` is exactly `CLAUDE.md` + `plans/docs-rails-release-gate-note.md`).
  The one security-relevant property of a docs change to `CLAUDE.md` is that it *instructs future
  agents* — and the correction moves in the safe direction: it removes a false justification for
  `RAILS_OVERRIDE=1` and tells the reader to earn the markers instead.
- The alternative shape — mirror core and let extension tests be advisory at the PR — is deliberately
  *not* taken and the file says why. If it is ever wanted, the missing piece is a release-time gate
  that runs the extensions' own suites, not a matching loosening.
- No behaviour change here: `.claude/hooks/rails_gate.py` in this repo is untouched.
