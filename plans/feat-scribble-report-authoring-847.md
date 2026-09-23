# Plan: feat/scribble-report-authoring-847

- **Branch:** `feat/scribble-report-authoring-847` (rebased onto `main` @ b397bad — carries #845 guard + #231 Report-Boards-proxy)
- **PR:** #232 (into ext main)
- **Status:** ✅ rebased onto #231; redesigned no-board path; re-verifying + re-acking

## Purpose
lotek#847 (ext-repo half) — expose the existing promote/report machinery in the UI. Most of #847 already
shipped (the ReportBoard rename #229, the #845 promote guard, cookie promote_job_ui/adopt_job,
engagement_by_core resolver #632, the source-jobs panel, the DnD board). Remaining ext work: a one-click
"add job to its report board" path a core job-page button can drive, copy/dead-end fixes, and a deep-link
manifest key. The core job-page button itself is #847-core.

## Done
- [x] Single-seam helper `_adopt_job_onto_board(db, board, job_id, actor)` (engagement_ui.py) — get_job →
      `assert_promote_anchor` (#845) → `mark_job_promoted` refuse-on-conflict → `promote_job`. `adopt_job`
      and the by-core POST twin call this ONE body (no fork of the #845 predicate).
- [x] Single board-create seam `_board_for_core(db, core_id)` — resolve-or-create (name+client DERIVED
      from `host.engagement_summaries()`, never trusted from input). `import_board` (#231's only create
      path) refactored onto it; `adopt_job_by_core` reuses it so one-click works board-or-not.
- [x] **One-click as a POST, not a GET-write.** `engagement_by_core` (GET) stays a pure #632 resolver
      (board → board, no board → the Report Boards list — #231 retired the standalone create form);
      a NEW **POST** `adopt_job_by_core` (`/engagements/by-core/<core_id>/adopt-job/<job_id>`) does the
      adopt — CSRF-protected (a write must not be a GET), write-gated, `can_operate_on`-gated (one 404).
      Board-absent → `_board_for_core` CREATES it (import-then-adopt, one request), then adopts; a 409
      rolls the fresh board back with it (commit is last). Declared in the scribble tenancy-gate classifier.
- [x] Copy/dead-end fixes: dashboard.html (removed stale "lands in WS3", actionable empty-state),
      engagement.html ("Report" kicker + scoped CSS). Visible copy only — no endpoint/url_for/var renames;
      board.js untouched. (engagements.html + engagement_new.html copy DROPPED — superseded by #231's
      Report-Boards-proxy rewrite; took #231's versions.)
- [x] `lotek-extension.toml`: `promoted_ref_path = "/scribble/engagements/{ref_id}"` — the /scribble prefix
      is REQUIRED (core's extensions.py substitutes {ref_id} verbatim, does NOT prepend url_prefix).
- [x] Tests: by-core one-click (adopts existing / GET-absent→list / cross-engagement 409 / refuse-on-
      conflict 409 / absent→CREATE+adopt) + the tenancy-gate classification entry; adopt_job's own tests
      are the refactor regression guard. Full scribble suite GREEN; ruff + pyrefly clean.

## Remaining
- [ ] Both reviews → ack; `--ack-tests`; PR #232 → self-merge → new ext tag (carries #845 guard + #847 UI).
- [ ] #847-core: the core job.html "Add to report" POST form + Playwright, and the single core re-pin to
      the new tag (which also lands #845-core).

## Notes / gotchas
- **Rebased onto #231 (Report Boards proxy).** #231 landed after this branch's base: `engagements` is now
  a VIEW over core engagements + `import_board` is the ONE create path; `engagement_new` is a redirect
  stub. So the original create-then-adopt hop (via engagement_new's hidden job_id) is DEAD — re-routed
  through `_board_for_core` (the import create seam). engagements.html + engagement_new.html copy edits
  were dropped in favour of #231's versions.
- **Security fix applied in review:** the initial implementation made `engagement_by_core` (GET) perform
  the adopt — a CSRF-able GET-side-effect. Moved to the POST twin above (the repo's writes are all POSTs).
- Standalone collect/browse view DEFERRED (would need a new `host.list_engagements` hook, cross-repo).
