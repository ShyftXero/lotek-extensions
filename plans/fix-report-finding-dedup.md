# fix/report-finding-dedup — Blade's report-finding critique fixes

- **Status:** releasing (fast path per Eli)

## Purpose
A rendered aggregation finding was bloated (3.1x the template height) and printed the host set 3x, with
a double-counted "Affected Assets (93)" for 52 hosts. Blade's critique → 4 fixes.

## Done (render_html.py + context.py)
- Skip folding the AFFECTED overlay into the asset list when a finding has children (children are the
  authoritative asset set) — kills the parent double-count + the "…and N more" bullet.
- Drop the `/proto` suffix from asset labels (one vocabulary) — fixes the flat single-host "(2)" double-count.
- Guard `_label_from_text` against the `_join_capped` sentinel / whitespace junk.
- Inline {{AFFECTED}}/{{TARGET_URL}} resolve to a count-reference ("N hosts (see Affected Assets below)")
  on a finding with children — no per-host wall in prose. Supersedes the host-list fallback for parents.

## Tests
- 2 new (no-double-count, count-reference) + 3 updated (/tcp→host:port). 121 report/render tests green, ruff clean.

## Follow-up still open: ext#311 (AFFECTED should aggregate URLs as a list at synthesis).
