# docs/drop-main-approval-requirement

- **Status:** ready for review

## Purpose

Required approving reviews on `main` were dropped from `1` to `0` on **this repo and
core** on 2026-08-27, at Eli's direction, while testing and the rails are being
overhauled. This branch makes this repo's `CLAUDE.md` describe what is now true.

Companion to ShyftXero/lotek#501. Tracking issue: ShyftXero/lotek#500.

## What changed on GitHub

`PATCH /repos/ShyftXero/lotek-extensions/branches/main/protection/required_pull_request_reviews`

| field | before | after |
| --- | --- | --- |
| `required_approving_review_count` | `1` | **`0`** |
| `require_last_push_approval` | `false` | `false` |
| `dismiss_stale_reviews` | `true` | `true` (gates nothing at `0`) |

Unchanged: PR still required for anyone protection binds — the bot cannot push to
`main` directly, though `enforce_admins: false` means the admin bypasses the PR
requirement too. Also `allow_force_pushes: false`, `allow_deletions: false`,
`required_status_checks: null`.

## Evals

| | before | after |
| --- | --- | --- |
| `required_approving_review_count` | 1 | **0** |
| PR #127 `mergeStateStatus` | review required | **`CLEAN` / `MERGEABLE`** |

The merge-state flip is the check that matters — reading the config back only proves
the write landed, not that it changed what GitHub enforces.

Docs grader:
`git grep -nIiE 'bars their approval|non-author reviewer|requires .*approving|1 approving' -- '*.md'`
returns only this branch's own past-tense sentence.

## Done

- `CLAUDE.md` — the bot-PR-authorship rationale (now about attribution, with the
  approval history recorded rather than deleted).
- `CLAUDE.md` — the `push-identity` gate description (a human-authenticated push no
  longer bars an approval; it only misattributes).

## Remaining — knowingly deferred

Stale claims survive in **code comments**, not docs. Fixing them makes a branch
non-docs-only, so they are left for the rails overhaul already underway — tracked as
checkboxes on ShyftXero/lotek#500:

- `.claude/hooks/rails_gate.py:205,274`
- `scripts/agent-push.sh:4`
- `scripts/gh-app-token.py:7`

Also left alone deliberately: `plans/feat-scribble-visible-client-ids.md:66` repeats
the old rule. Per-branch plans are records of their time, not live guidance, and
rewriting shipped ones would falsify the record.

None of this changes behaviour — the bot-push plumbing works exactly as before and is
still the right default for attribution.

## Notes / gotchas

- The `push-identity` gate itself is **unchanged and still active**. Only its stated
  justification moved; it is still the right default, now for attribution rather than
  to keep an approval legal.
- Restoring the gate is one field — see the command in core's `CLAUDE.md` gates table.
