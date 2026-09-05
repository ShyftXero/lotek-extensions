# Plan: feat/scribble-first-job-cta-631

- **Branch:** `feat/scribble-first-job-cta-631`  (worktree: `.claude/worktrees/s631-empty-state-cta`,
  stacked off `feat/scribble-adopt-a-job-630` → `feat/scribble-source-jobs-panel-629`)
- **PR:** not opened yet (orchestrator owns the PR/gate steps)
- **Status:** 🟢 code + tests committed locally; PR not opened

## Purpose
First-job empty-state CTA (#631) on the engagement board. When NO scan job has been promoted into an
engagement (`host.list_jobs` returns `[]`), the Source-jobs panel (#629) shows a call-to-action that
deep-links to the CORE job-create page (`/assessments/new`), so an operator can run the first scan for a
fresh engagement. Purely additive to #629/#630 — an engagement WITH source jobs shows no empty-state CTA
and the #629 panel + #630 picker still render.

## Evals
- **Hypothesis:** a zero-source-jobs engagement renders the CTA with the correct core deep-link
  (`href="/assessments/new"`); an engagement with ≥1 source job renders the job and NOT the CTA.
- **Graders:** `scribble/tests/test_first_job_cta.py` (route status + rendered board given stub data) +
  the full `scribble/tests` suite green.
- **RED baseline:** with the CTA `{% if %}` neutralized (`False and …`) → `test_empty_engagement_...`
  RED (CTA markup absent); with the `not source_jobs` guard dropped (CTA unconditional) →
  `test_engagement_with_jobs_hides_the_cta` RED (CTA present when jobs exist). Both GREEN with the shipped
  condition — so each assertion guards a distinct half of the behaviour.
- **Verdict:** GREEN.

## Done
- [x] Plan committed.
- [x] Empty-state CTA in the Source-jobs panel (`engagement.html`, additive): gated
      `{% if not source_jobs and scribble_host_mounted and scribble_can_write %}` — only when the panel
      is empty, a host is mounted (standalone Scribble has no core scan surface to link to), and the
      viewer can write. Deep-links `/assessments/new` (core's own "New Scan" route,
      `lotek/src/app/routes/jobs.py:213` — root-relative so it resolves under any scribble mount prefix,
      like the manifest paths).
- [x] `tests/test_first_job_cta.py` — renders-when-empty / hidden-when-jobs-present. Red before (both
      halves, see Evals), green after.

## Remaining
- [ ] PR steps (orchestrator): `/security-review` + `/adversarial-reviewer` on
      `git diff origin/main...HEAD`, `--ack-*`.
- [ ] MOUNTED integration (human PR-gate): core + #632 + scribble, the mounted engagement-board UI e2e
      (`LOTEK_E2E_UI=1`) — this box has no playwright chromium, so the deterministic route/render test
      above stands in; the mounted UI e2e is owed before a `main` merge.
- [ ] Stacked: merge #629 → #630 → this, in order.

## Notes / gotchas
- `source_jobs` is the ONE derived source-jobs view (`host.list_jobs(engagement, actor)` →
  `scribble/host.py`), reused verbatim from #629 — the CTA is a pure presentation branch on the same
  value, no second computation of "has this engagement been fed".
- No `remove_job_adoption` / un-adopt here despite #630's plan note guessing #631 would be that — this
  ticket is scoped to the empty-state CTA only.
