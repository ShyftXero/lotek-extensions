# fix(scribble): adopt fails with a 500 on a long finding URL (#402)

- **Status:** In review.
- **Issue:** ShyftXero/lotek-extensions#402.
- **Cause (from prod logs):** `POST /scribble/engagements/<board>/adopt-job/<job>` returned 500 with
  `psycopg.errors.StringDataRightTruncation: value too long for type character varying(1024)` on
  `UPDATE scribble_findings SET target_url=...`. A dalfox crawl result produced a comma-joined pair of
  URLs longer than the column, so the promotion transaction aborted and the UI showed
  "Could not adopt that job."
- **Fix:** every host-supplied text column on `scribble_findings` is now TEXT: `target_url` (was 1024) and
  `title` (was 512, while the host allows 1024). Migration `e9c4a2b7f310` widens both on an existing
  deployment. Truncating was rejected: both are evidence.
- **Also fixed:** `Artifact.caption` (VARCHAR(255)) was formatted as `Scan screenshot - {url}`, so a URL
  over ~230 chars aborted the same promotion. Captions are display labels, so they are clipped with an
  explicit ellipsis while the full URL stays on the finding.
- **Evidence:** hermetic model guard plus a Postgres-gated migration test that reproduces the refusal at
  the old width and proves the repair. SQLite cannot prove this class (no length enforcement).
- **Not the cause:** CSRF. The host's fetch wrapper injects `X-CSRFToken` for mutating same-origin
  requests, and prod logged a 500 from inside `promote_job`, not a 400 from the CSRF gate.
- **Remaining:** the sibling report "Un-adopt + delete findings does not remove the block" is separate and
  still open on #402; prod shows the destroy route returning 302 with the findings retained.
