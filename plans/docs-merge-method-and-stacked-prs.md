# Plan: docs/merge-method-and-stacked-prs

- **Branch:** `docs/merge-method-and-stacked-prs`  (worktree: `.claude/worktrees/attackpath-kit`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose

`CLAUDE.md` instructs **squash-merge**. Squash merge is **disabled** on this repository, so the
instruction cannot be followed and the first attempt to follow it fails with
`GraphQL: Squash merges are not allowed on this repository`.

Verified against the API rather than inferred:

```
$ gh api repos/ShyftXero/lotek-extensions --jq '{squash:.allow_squash_merge, merge:.allow_merge_commit, rebase:.allow_rebase_merge}'
{"merge":true,"rebase":true,"squash":false}
```

While correcting it, record the stacked-PR trap that cost real recovery work landing #161/#162: deleting
the base branch of an open stacked PR **closes** that PR.

## Scope

Two stale `squash-merge` references (`CLAUDE.md:34` and `:250`) and one new paragraph on stacked PRs.
Nothing else — the approval-requirement passages at `:58-60` and `:124-125` are **already correct** (they
record that approvals on `main` are now `0` and that `require_last_push_approval` is off), confirmed
against `gh api repos/.../branches/main/protection`:
`required_approving_review_count: 0`, `require_last_push_approval: false`.

## Done
- [x] This plan, committed first
- [x] `CLAUDE.md` branch-per-change-type recipe: `squash-merge` → merge, plus the disabled-squash note
      and the API command to check rather than remember (command run as written, output verified)
- [x] `CLAUDE.md` "shipping a change back to lotek" step 1: same correction, plus the clarification that
      a tag in this repo is not a deploy — it only gives lotek something to pin
- [x] New `### Stacked PRs` subsection: the `--delete-branch` trap, the order-forced recovery, why
      `--merge` beats `--rebase` for the lower PR, and scoping each review to its own diff

## Remaining
- [ ] Nothing.

## Notes / gotchas

- **Merge commit, not rebase, for a stack.** A merge commit keeps the lower PR's head a real ancestor of
  `main`, so the upper PR's merge-base stays valid and it retargets cleanly. Rebase-merge rewrites SHAs,
  which strands the upper PR's base and would need a force-push to fix — and force-pushing is barred
  (`allow_force_pushes: false` on `main`, and agents must not force-push anywhere).
- **`required_linear_history` is `false`**, so merge commits are permitted; this is not a preference the
  docs can state as forbidden.
- The `--delete-branch` trap is *asymmetric and hard to reverse in the wrong order*: a **closed** PR
  cannot be retargeted, and a PR whose base is **missing** cannot be reopened. So the recovery order is
  forced: restore the base ref → reopen → retarget → merge → delete the ref again.
