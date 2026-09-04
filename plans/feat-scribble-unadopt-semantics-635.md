# Plan: feat/scribble-unadopt-semantics-635

- **Branch:** `feat/scribble-unadopt-semantics-635`  (worktree: `.claude/worktrees/s635-unadopt`,
  stacked off `feat/scribble-first-job-cta-631` → …630 → …629)
- **PR:** not opened yet (orchestrator owns the PR/gate steps)
- **Status:** 🟢 code + tests committed locally; PR not opened

## Purpose
Un-adopt semantics (#635) — the reverse of adopt-a-job (#630). Add an un-adopt affordance to the
Source-jobs panel with TWO paths:
- **(a) link-only** — `host.remove_job_adoption(job)` clears the promotion link, keeps every promoted
  finding untouched (the operator's edits survive).
- **(b) destructive** — also removes the findings that job enriched, gated behind a PREVIEW (list the
  exact ids that would be removed) and an audit row (`_audit`, api_pat.py).

## Evals
- **Hypothesis:** link-only clears the link (job leaves the panel) but every finding stays; destructive
  preview lists exactly the enriched finding ids; destructive confirm removes exactly those (leaving
  non-enriched findings) and writes one audit row.
- **Graders:** `scribble/tests/test_unadopt_job.py` (route status + rendered/JSON end-state given stub
  data) + `scribble/tests/test_unadopt_single_predicate.py` (grep guard) + full `scribble/tests` green.
- **RED baseline:** routes absent → 404 on unadopt/preview/destroy; `finding_is_enriched`/
  `enriched_findings` absent → ImportError. Guard test RED if a second call site of `finding_is_enriched`
  appears.
- **Verdict:** GREEN.

## Done
- [x] Plan committed.
- [x] `host.remove_job_adoption(job_id, actor)` — the link-clearer host seam (core #632), mirror of
      `mark_job_promoted`; `[]`/no-op when unmounted.
- [x] `promote.finding_is_enriched(finding, job_finding_ids)` — THE single predicate ("did job J enrich
      this finding": `source_finding_id` membership) — plus `promote.enriched_findings(engagement,
      job_finding_ids)`, its ONE caller, shared by preview and destroy so the two can never disagree.
- [x] Three UI routes in `engagement_ui.py`: `unadopt_job` (link-only), `unadopt_job_preview` (JSON list
      of doomed findings), `unadopt_job_destroy` (delete exactly those via `findings_service.delete_finding`
      + `_audit` + clear link). All `host_can_write`-gated; unknown/unmounted engagement → 404.
- [x] Additive `engagement.html` edit — per source-job un-adopt controls (link-only form + destructive
      button with preview/destroy data-urls), gated `scribble_can_write`. `board.js` handler for the
      preview→confirm→destroy path.
- [x] Stub host extended: `remove_job_adoption` + `unadopt_calls`; wired into `_wire_stub_host`.
- [x] Tests: `test_unadopt_job.py` (link-only keeps findings; preview lists right ids; destroy removes
      exactly those + audit row + non-enriched survive; render additive) and
      `test_unadopt_single_predicate.py` (single call path). Red before, green after.

## Remaining
- [ ] PR steps (orchestrator): `/security-review` + `/adversarial-reviewer` on
      `git diff feat/scribble-first-job-cta-631...HEAD`, `--ack-*`.
- [ ] MOUNTED integration (human PR-gate): core + #632 + scribble, mounted engagement-board UI e2e
      (`LOTEK_E2E_UI=1`) — this box has no playwright chromium, so the deterministic route/render/JSON
      tests stand in; the mounted UI e2e (and the real `remove_job_adoption`/host-side finding-id
      resolution) is owed before a `main` merge.
- [ ] Stacked: merge #629 → #630 → #631 → this, in order.

## Notes / gotchas
- **One predicate, one home:** `finding_is_enriched` lives in `promote.py` (next to promotion, which
  owns `source_finding_id`). Preview and destroy both reach it through `enriched_findings` — the only
  caller — so "what the preview lists" and "what the delete removes" are the SAME set by construction.
  `test_unadopt_single_predicate.py` fails if a second inline membership test reappears.
- **Destructive removes CHILDREN, not synthesized parents:** a job's scan findings become child/flat
  rows carrying `source_finding_id`; a resolved-template PARENT write-up carries none, so
  `finding_is_enriched` excludes it — "removes exactly those the job enriched", author-owned write-ups
  survive. `findings_service.delete_finding` takes each row's artifacts + detaches any children.
- **`remove_job_adoption` is link-only in BOTH paths** — the destructive path deletes findings itself,
  then clears the link. The host seam never touches findings (no data loss is the host contract).
- Core #632 (`host.remove_job_adoption`, host-side finding-id resolution) is built + invariant-green but
  not yet on main; the scribble stub host provides these, so these tests are hermetic.
