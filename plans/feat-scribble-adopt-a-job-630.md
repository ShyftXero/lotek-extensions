# Plan: feat/scribble-adopt-a-job-630

- **Branch:** `feat/scribble-adopt-a-job-630`  (worktree: `.claude/worktrees/s630-adopt-job`, stacked
  off `feat/scribble-source-jobs-panel-629`)
- **PR:** not opened yet (orchestrator owns the PR/gate steps)
- **Status:** 🟢 code + tests committed locally; PR not opened

## Purpose
Adopt-a-job UI (#630) — the write half of the Source-jobs panel (#629, which is read-only). A cookie
route `POST /engagements/<id>/adopt-job/<job_id>` LINKS a scan job into this engagement
(`host.mark_job_promoted`, core #632) and pours its findings onto the board (`promote.promote_job`), plus
a picker in the panel to drive it from the browser. The link is REFUSE-ON-CONFLICT: a job already
promoted into another engagement surfaces a 409 and is never re-pointed.

## Evals
- **Hypothesis:** adopting an unadopted job links it (the panel then lists it via `host.list_jobs`);
  adopting a job already promoted elsewhere returns 409 and re-points nothing; an unknown/forbidden job
  is 404 with no leak.
- **Graders:** `scribble/tests/test_adopt_job.py` (route end-state + rendered panel) + the full
  `scribble/tests` suite green. RED baseline: route absent → 404 for both success + conflict tests.
- **Verdict:** GREEN — `test_adopt_job.py` red before (404), green after; `test_source_jobs_panel.py`
  still green (stub change is compatible).

## Done
- [x] Plan committed.
- [x] Route `scribble.adopt_job` (`engagement_ui.py`): write-gated, `get_job` tenancy (404 no-leak),
      `mark_job_promoted` as the refuse-on-conflict gate (409, pour nothing), then `promote_job`.
- [x] Source-jobs panel picker (`engagement.html`, additive under `scribble_can_write`) + board.js-style
      JS (`board.js` `initAdoptJob`) that POSTs to the route and surfaces 409/404.
- [x] StubHost (`tests/conftest.py`): `mark_job_promoted` now refuses-on-conflict + idempotent re-affirm
      of the same target + writes the link so `list_jobs` reflects it (one write feeds one read).
- [x] `tests/test_adopt_job.py` — link / 409-no-repoint / 404-no-leak / picker renders. Red before code.

## Remaining
- [ ] PR steps (orchestrator): `/security-review` + `/adversarial-reviewer` on
      `git diff feat/scribble-source-jobs-panel-629...HEAD`, `--ack-*`, and the MOUNTED integration —
      core + #632 + scribble, `lotek/tests/test_e2e_webui.py` under `LOTEK_E2E_UI=1` (this box has no
      playwright chromium, so the deterministic route/render test above stands in; mounted UI e2e owed
      before a `main` merge).
- [ ] Stacked: merge #629 first, then this.

## Notes / gotchas
- `mark_job_promoted` returns `False` on conflict (no raise), per #632. The route treats `get_job`
  success as "actor may view + write here", so a subsequent `False` is a real conflict → 409. Ordering
  is mark-FIRST so the refusal can gate the pour (the existing `promote_job_ui` marks best-effort AFTER
  pouring and swallows conflicts — kept as-is, out of scope).
- No host hook lists *promotable* jobs yet, so the picker is an id input, not a `<select>` (same as
  `promote_job_ui`'s note). A `<select>` is the follow-on once such a hook exists.
- Un-adopt (link-only, `host.remove_job_adoption`) is #631, not here.
