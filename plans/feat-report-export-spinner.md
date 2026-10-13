# feat/report-export-spinner
- **Status:** releasing (fast path per Eli)
Server PDF/DOCX export links were plain <a href> — a slow LibreOffice PDF render gave no feedback. Now the
⬇ PDF / ⬇ DOCX links (data-export) are intercepted: spinner + "Rendering…", fetch as a blob, trigger the
download (filename from Content-Disposition), restore on done; a failure alerts instead of silently hanging.
JS (node --check OK) + spinner CSS in render_html. Test + 21 report tests green.
