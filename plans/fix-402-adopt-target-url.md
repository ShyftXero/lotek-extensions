# fix(scribble): adopt fails with a 500 on a long finding URL (#402)

- **Status:** In review.
- **Issue:** ShyftXero/lotek-extensions#402.
- **Cause (from prod logs):** `POST /scribble/engagements/<board>/adopt-job/<job>` returned 500 with
  `psycopg.errors.StringDataRightTruncation: value too long for type character varying(1024)` on
  `UPDATE scribble_findings SET target_url=...`. A dalfox crawl result produced a comma-joined pair of
  URLs longer than the column, so the promotion transaction aborted and the UI showed
  "Could not adopt that job."
- **Fix:** `target_url` (was 1024) is TEXT, because a scanner writes it and a dalfox crawl result
  exceeded the width. `title` (was 512) is TEXT too, because the finding-detail form lets an operator type
  a title with no length check (core's `Finding.title` is String(255), so the promotion path itself could
  not have overflowed 512). Migration `e9c4a2b7f310` widens both on an existing deployment. Truncating was
  rejected: both are evidence.
- **API boundary:** `api_pat._COLUMN_MAX_LEN` no longer caps `title` or `target_url`, so a value the
  promotion path stores cannot then be refused by its own PATCH.
- **Still bounded:** `target_host` (255), `target_port` (16), `category` (255), `cvss_vector` (255) remain
  VARCHAR and stay capped at the API. `target_host` is the one host-supplied value that can still be
  truncated; a 255+ char host is out of scope here.
- **Also fixed:** the screenshot caption was formatted as `Scan screenshot - {url}` with no bound.
  `Artifact.caption` is Text today, so the clip added here is a no-op guard on the formatter rather than a
  schema repair; captions are display labels and stay clipped while the full URL lives on the finding.
- **Evidence:** hermetic model guard plus a Postgres-gated migration test that reproduces the refusal at
  the old width and proves the repair. SQLite cannot prove this class (no length enforcement).
- **Not the cause:** CSRF. The host's fetch wrapper injects `X-CSRFToken` for mutating same-origin
  requests, and prod logged a 500 from inside `promote_job`, not a 400 from the CSRF gate.
- **Sibling fix:** "Un-adopt + delete findings does not remove the block" is a separate defect, fixed in
  PR #404: the destroy route returned 302 without deleting whenever the adoption link had already been
  cleared.
