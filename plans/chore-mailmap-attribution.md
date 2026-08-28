# chore/mailmap-attribution

- **Status:** ready for review

## Purpose

61 commits here were authored under a stale work address, `emcrae@synoptek.com`,
instead of `shyft@shyft.us`. Add a `.mailmap` so git reports them under the
canonical identity without rewriting history. Companion to ShyftXero/lotek#499,
which does the same for the 170 affected commits in core.

## Background

GitHub resolves a commit to an account by the **git author email**, so these
commits render on github.com under a separate personal account rather than under
`ShyftXero`. The commits are genuine agent work committed by `lotek-agent[bot]`.
Repository Activity API confirms that account has **0** pushes, branch creations
or force-pushes in this repo — this was misattribution, not access.

Root cause: 8 per-worktree `config.worktree` files in this repo (plus 11 in core)
carried `author.email = emcrae@synoptek.com`. Global and repo-local config were
always correct. The address appears in **zero tracked files**.

## Done

- All 8 `config.worktree` files here rewritten to `author.email = shyft@shyft.us`
  (19 across both repos, verified 0 remaining).
- Added `.mailmap`.

## Evals

Grader: `git shortlog -sne --all`, before/after on the same scope.

| | stale-identity lines | `Eli McRae <shyft@shyft.us>` |
| --- | --- | --- |
| baseline (`.mailmap` absent) | 61 | 3 |
| after (`.mailmap` present) | **0** | **64** |

61 + 3 = 64 — exact fold-in, no commit gained or lost. Negative check run: removing
`.mailmap` brings the stale line back, restoring it removes it. Escape hatch
verified: `--no-use-mailmap` still finds all 61, the default finds 0.

## Remaining

**`.mailmap` does not fix github.com.** GitHub does not read it. The web UI is
corrected by moving `emcrae@synoptek.com` off the other account and onto
`ShyftXero` in Settings -> Emails, which reattributes all 231 commits across both
repos at render time with no history rewrite. Tracked in ShyftXero/lotek#498.

## Notes / gotchas

- `log.mailmap` defaults to true since git 2.6, so this needs no per-user config.
- A `git filter-repo` rewrite was rejected: it would force-push both protected
  `main`s, orphan 36 local worktrees, break every open PR and the release tags prod
  tracks, and invalidate every `extensions/` submodule gitlink in core's history,
  which pins this repo's SHAs.
