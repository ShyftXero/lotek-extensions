# Plan: fix/scribble-report-evidence-render

- **Branch:** `fix/scribble-report-evidence-render`  (worktree: `.claude/worktrees/e-evidence`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose
Closes #40, #54, #61, #62. Image/file evidence uploaded to an engagement (finding_id NULL) or to a
nested child finding never renders anywhere in the report (HTML, docx, or zip). Separately, the
inline-content-image budget silently blanks an image with no note (#61), and the zip export applies
the same appendix item cap to the FILES it ships, not just the listing (#62). This branch builds the
full evidence-rendering solution (engagement-level appendix + child galleries) across
render_html.py/render_docx.py, honest inline-missing chips, and a zip-exempt appendix cap — plus the
minor artifacts_api.py finding_id-tenancy echo from #40's mech3.

## Done
- [x] Verified current main state (children render text-only in both renderers; ReportContext has no
      `artifacts` field; no appendix/cap machinery exists yet; render_html.resolve_inline still emits
      silent _BLANK_PIXEL; artifacts_api.create_artifact stores finding_id unvalidated).
- [ ] context.py: `ReportContext.artifacts` (engagement-level evidence)
- [ ] render_html.py: child galleries, evidence appendix (zip-exempt cap), honest inline-missing chip
- [ ] render_docx.py: child galleries, evidence appendix
- [ ] artifacts_api.py: finding_id tenancy validation + echo
- [ ] Tests (red before / green after) in test_report_html.py, test_report_docx.py, test_artifacts.py
- [ ] Fast-tier checks (ruff, pyrefly, targeted pytest)
- [ ] PR opened

## Remaining
(see Done above)

## Notes / gotchas
- Two separate create-artifact routes exist: `artifacts_api.py` (`POST /scribble/api/artifacts`,
  session-cookie UI upload) has NO finding_id/engagement tenancy check — this is the actual #40-mech3
  gap. `api_pat.py`'s PAT machine route (`POST .../engagements/<id>/artifacts`, tested in
  test_machine_artifacts.py) ALREADY nulls a cross-engagement finding_id (from the tenancy branch) —
  only its response body doesn't echo finding_id/finding_id_dropped, which is out of scope for this
  branch's fix (that route isn't in the plan's file list). Fix + test artifacts_api.py + test_artifacts.py.
- Do NOT reintroduce a shared byte budget across gallery + inline images — keep them unbudgeted; only
  cap the engagement-level appendix (inline mode only; zip mode exempt per #62).
- Preserve `test_render_report_html_renders_nested_children_compactly` /
  `test_render_report_docx_renders_nested_children_compactly` exactly (ordering + no-content-leak
  assertions).
