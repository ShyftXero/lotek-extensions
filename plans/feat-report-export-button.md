# feat/report-export-button
- **Status:** releasing (fast path per Eli)
## Purpose
No UI control reached the report export routes. Add server-side ⬇ PDF / ⬇ DOCX download links to the
report toolbar, gated on export_url (live preview only — the exported deliverable passes none, so a
downloaded file has no dead app links). Threaded render_report_html -> _render_document -> _render_header.
## Done
- render_html.py: export_url param through the 3 fns; tb-actions gains ⬇ PDF / ⬇ DOCX; browser button
  relabelled ⎙ Print. report_html_api.py /report route passes export_url=url_for(engagement_report_export).
- test_report_affected_and_repro.py: links present with export_url, absent in the deliverable. 52 report tests green.
