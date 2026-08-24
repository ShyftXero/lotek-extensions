# Plan: feat/scribble-activity-appendix

- **Branch:** `feat/scribble-activity-appendix` (worktree: `.claude/worktrees/activity-appendix`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose
Part 4 of 4 for lotek#442. Make the engagement **activity log** an OPTIONAL report appendix — requestable,
not generated inline — toggled by including the `activity_log` block in a report template, with its own
TOC entry (mirrors the existing `evidence` appendix). The activity trail is built entirely from
scribble's OWN timestamped data (EngagementFinding/Artifact/EngagementDiagram/Engagement `created_at`
via TimestampMixin) — no cross-seam sourcing. Renders "when each finding was added / evidence uploaded /
diagram created", chronologically.

## Evals
- **Hypothesis:** a template including `activity_log` renders an "Activity Log" appendix with `id=
  sec-activity` and its own TOC entry, populated chronologically from the engagement's timestamps; a
  template WITHOUT the block renders no such section (opt-in). origin/main has no activity appendix.
- **Mode / aggression:** 2 (recorded)
- **Capability evals** (must newly pass):
  - [ ] `_build_activity_log` orders finding/evidence/diagram events by timestamp — `pytest tests/test_activity_appendix.py`
  - [ ] template with `activity_log` → `sec-activity` in doc + "Activity Log" in TOC — same
  - [ ] template without it → no `sec-activity` (opt-in) — same
  - [ ] empty activity → appendix + TOC entry omitted (like evidence) — same
- **Regression evals:** `test_report_cover_and_toc.py` (TOC-completeness pin) stays green.
- **Graders:** scribble test suite + `--ack-invariants` (ext-tagged skip honestly).
- **Verdict:** (filled after candidate run)

## Done
- [x] Plan committed first.

## Remaining
- [ ] `ActivityEntry` + `ReportContext.activity_log` + `_build_activity_log` (context.py)
- [ ] `"activity_log"` in BLOCK_KEYS (opt-in; NOT in the default template)
- [ ] `_render_activity_appendix` + dispatch + `_toc_entries` branch (render_html.py)
- [ ] tests (incl. TOC-completeness) red-then-green
- [ ] reviews + acks + PR (cross-repo gate: lotek-rooted session's --ack-invariants can't be earned by
      ext → RAILS_OVERRIDE, logged, per the worktree-gate memory)

## Notes / gotchas
- "Checkbox to include" = `activity_log` present in a template's `blocks` tuple; TOC entry appears only
  when the block is in the template AND `ctx.activity_log` is non-empty (mirrors `evidence`).
- ReportContext is a frozen contract — add the field with a default so existing callers are unaffected.
- Do NOT add `activity_log` to the default template's blocks — it must stay opt-in.
