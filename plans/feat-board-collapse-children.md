# feat/board-collapse-children (+ auto-section by assessment scope)

- **Status:** built + tested, releasing (gate overridden per user — fast path)

## Purpose
Two batched board/report changes:
1. **Collapse children under parent** on the board — per-host children were rendered as individual cards
   (198 for 9 vulns = the "not grouping" complaint). Hide nested children, show "N hosts" on the parent.
2. **Auto-section by assessment scope** — a job's findings land in the report section matching its scope
   (external -> "External", internal -> "Internal"), find-or-creating the AssessmentType + FindingGroup.

## Done
- engagement_ui.py: board context computes nested_ids + child_counts; board lists exclude nested
  children. _adopt_job_onto_board calls auto_section_job_findings. New `resection_jobs` route (backfill).
- findings_service.py: `section_name_for_scope` + `auto_section_job_findings` (sections a job's children
  AND their parent shell; non-clobbering, idempotent).
- engagement.html: "N hosts" pill on parents; "Auto-section by assessment type" button in Source jobs.
- tests: collapse + 2 auto_section unit tests. 97 related tests green, ruff clean.

## Decisions / ceiling
- Scope comes from `JobDTO.scope_type` (already exposed) — NO core change needed. Webapp/AD/Cloud SURFACE
  is NOT in scope_type (needs the job's run-type template category, which current jobs don't set), so
  those are deferred until that signal exists rather than guessed from a job name.
- Parent shells carry no source_facts.job_id, so a matched child drags its parent into the section.
- Existing UKG findings: click "Auto-section by assessment type" (backfill) after deploy.
