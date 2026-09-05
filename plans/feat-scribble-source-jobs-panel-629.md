# Plan: feat/scribble-source-jobs-panel-629

- **Branch:** `feat/scribble-source-jobs-panel-629`  (worktree: `.claude/worktrees/s629-source-jobs`, off `origin/main`)
- **PR:** not opened yet (link 1 of a stacked chain #629→#630/#631/#635; opened by the human)
- **Status:** 🟢 ready to merge

## Purpose
Engagement → jobs REVERSE view (#629). The board can promote a scan job's findings in
(`host.mark_job_promoted`); nothing showed the reverse — which jobs fed this engagement. Add a
"Source jobs" panel to `engagement.html` driven by the new `host.list_jobs(engagement)` seam
(consuming core #632's reverse-view host hook). This panel is the insertion point #630/#631/#635
build richer content onto.

## Evals
- Guard: `scribble/tests/test_source_jobs_panel.py` — GET the board and assert the RENDERED end-state.
  - 2 promoted jobs → both refs + each `promoted_at` render. (RED before panel: FAILED 3/3)
  - none → explicit empty state. Different engagement's job does not bleed in (ref_id scoping).
- Baseline: on `origin/main` the board renders without the panel; the 3 guards fail. After: 3 pass.

## Done
- [x] `scribble/host.py`: `list_jobs(engagement, actor_obj)` seam wrapper (mirrors `mark_job_promoted`,
      `[]` when unmounted).
- [x] `engagement_ui.py::engagement_board`: `source_jobs = host.list_jobs(engagement, current_actor())`
      into the render context.
- [x] `templates/scribble/engagement.html`: additive "Source jobs" panel (Jinja `for…else` empty state).
- [x] `tests/conftest.py`: extended stub host — `add_promoted_job()` + `list_jobs()` + `extras["list_jobs"]`.
- [x] Guard red-before / green-after; scribble fast suite green.

## Remaining
- [ ] Human PR-gate step: the REAL mounted integration (core + #632 + scribble,
      `lotek/tests/test_e2e_webui.py` + `LOTEK_E2E_UI`). NOT attempted here (no core boot).
- [ ] Mounted-UI Playwright assertion (this box has no chromium) — deterministic route/template-render
      test stands in for it.

## Notes / gotchas
- **Seam signature is a best-effort mirror.** `list_jobs` calls the host hook as
  `hook(actor_obj, extension="scribble", ref_id=engagement.id)` — the symmetric inverse of
  `mark_job_promoted(job_id, actor_obj, extension=, ref_id=)`. Core #632 isn't on this machine;
  reconcile the exact signature against #632 at mount time (the human PR-gate step).
- Single home: the reverse view lives ONLY in `host.list_jobs`; the route calls it once and the
  template iterates. No inline authz/enrichment copy. Tenancy (`user_can_view_job`) is the host's
  concern, proven mounted.
- Ordering is the host's concern — the panel renders whatever order the hook returns (#630/#631 refine).
