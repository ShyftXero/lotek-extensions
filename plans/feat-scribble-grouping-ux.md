# Plan: feat/scribble-grouping-ux

- **Branch:** `feat/scribble-grouping-ux` (off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose

UX pass on the report board's grouping pivots (a /ui-ux + /grill session with Eli). Four agreed changes:

- **Q1 — name the axis.** The tab bar becomes **"Group by: Section | Vulnerability | Host"**; the board's
  "Ungrouped" bucket → **"No section"**; "Add a group" → **"Add a section"**. Removes the collision where
  section placement (a report-layout concept) and the by-vuln/by-host pivots (data aggregation) both wore
  the word "group".
- **Q2 — bulk-assign from a pivot.** Each vuln/host bucket gets an **"Assign all → \<section\>"** control
  that moves ALL its findings to a report section (reuses the batch-move endpoint) — a cross-axis move
  ("put all EternalBlue in Internal"). The old multi-select bulk bar moved INSIDE the Section panel so it
  hides in the pivots (was an inert control there).
- **Q3 — over-tall + DataTable.** Drop the redundant "Findings" column; make each affected host (by-vuln) /
  each vuln title (by-host) the link to ITS finding. Per-bucket **"see more…"** collapses long host lists
  (first 8, "+N more"). Both pivot tables are **DataTables** (sort/filter/paginate when mounted; plain
  table standalone). By-vuln dropped **18,751px → 1,163px**; by-host 11,277 → 2,145px.
- **Q4 — the edge.** A host with two findings of the same vuln links the first and appends "·+1".

## Done
- [x] `engagement.html`: terminology; bulk bar moved into `#scribble-view-board`; both rollup tables
      rewritten (column-merge, hosts-as-links, see-more, `data-dt`, per-bucket Assign); CSS + delegated JS
      (see-more reveal; Assign → confirm → POST `move_findings` → reload).
- [x] Tests: `test_board_finding_grouping.py` (group-by tabs, hosts-as-links + Findings column dropped,
      see-more, Assign carries finding ids, bulk bar inside the Section panel); `test_board.py` copy
      assertions updated to the new terminology. Full scribble suite **1740 passed** (1 unrelated e2e
      resilience flake, green in isolation).
- [x] Verified live on the demo: heights, the three tabs, hosts-as-links, see-more reveal, and the Assign
      POST (correct body + host-injected CSRF, aborted so the demo is untouched).

## Remaining
- [ ] Reviews + acks → PR → merge → tag. Core re-pin — **first real use of the re-pin test lane** (its
      `--ack-tests` runs the mounted-ext slice, not the full core suite).

## Notes / gotchas
- DataTables is available only MOUNTED (core base.html loads it); standalone degrades to a plain table.
  It inits on hidden panels — `autoWidth:false` handles that. It snapshots tbody at init, so see-more only
  reveals content ALREADY in the cell (a CSS toggle), never adds rows.
- `move_findings` takes `{finding_ids, group_id(null=No section), order_index}`; CSRF injected by the host
  fetch wrapper. Assign handlers are delegated on `document` to survive DataTables pagination.
- The report DELIVERABLE (`render_html.py`) keeps its static stacked rollup — a JS toggle/DataTable would
  vanish from the PDF; unaffected by this board-only change.
