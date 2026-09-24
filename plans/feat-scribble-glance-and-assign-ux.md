# Plan: feat/scribble-glance-and-assign-ux

- **Branch:** `feat/scribble-glance-and-assign-ux` (off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose

Two UX defects Eli caught after the grouping-view work landed:

- **A — the Assign dropdown was browser-native.** The `.rl-assign-sel` selects sit in the rollup tables,
  OUTSIDE any `.card`, so scribble.css's `.card select` base rule never reached them — they rendered as a
  light-grey native box against the dark theme (measured bg `rgb(43,42,51)` vs the app's `rgb(13,23,34)`).
  Gave them the field styling explicitly. Only visible in the By-vulnerability / By-host tabs — which is
  exactly why the walk crawler missed it (it doesn't click JS tabs; see the core walk.py follow-up).
- **B — the report's "Findings at a glance" enumerated every host.** One row per top-level finding =
  428 rows / ~18k px on a real engagement — the opposite of at-a-glance. Regrouped the exec-summary index
  by VULNERABILITY (title): one row per vuln with a host COUNT, worst-severity-first, CVE (omit-when-empty)
  + KEV flag kept. **428 rows → 15; 18,635px → 711px.** The per-host detail lives in the finding cards and
  the By-host rollup; per-finding Status/CWE/CVSS drop from the index (they don't collapse per-vuln — they
  stay on the cards + the disposition/severity rollups).

## Done
- [x] `engagement.html`: `.rl-assign-sel` field styling.
- [x] `reporting/render_html.py`: `_findings_index` groups by vulnerability (host count, not per-host).
- [x] Tests: collapse test (`test_findings_at_a_glance_groups_by_vulnerability_not_per_host`); updated the
      #618 status-column and #625 CWE-column index assertions (status/CWE are card chips now, not index
      columns). Full scribble suite green.
- [x] Verified live: assign bg now matches the field style; the report index is 15 rows / 711px.

## Remaining
- [ ] Reviews + acks → PR → merge → tag → core re-pin (via the re-pin lane).
- [ ] (core, separate) walk.py: click each `[data-view]`/tab and re-run the detectors on the revealed
      panel, and stop blanket-exempting a report's over-tall — flag an over-tall INDEX/summary table. This
      is the /ui-ux coverage gap that let both defects through.
