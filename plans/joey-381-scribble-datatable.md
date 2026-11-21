# Plan: joey/381-scribble-datatable

- **Branch:** `joey/381-scribble-datatable`  (worktree: `~/lotek-extensions.joey-381-scribble-datatable`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose
ext#381 wants the scribble Report Boards list (`/scribble/engagements`) to use the shared datatable
(client-side filter, sortable columns, per-page paging) the ext#380 relay list uses, not a bespoke
table. The markup conversion had already landed on `main` in `6bec433` (via epic #391) but the surface
carried no test, so nothing distinguished it from the plain table it replaced and nothing pinned the
standalone plain-table fallback. This branch pins the #380 parity contract for this list.

## Done
- [x] Confirmed `/scribble/engagements` renders `table[data-dt]` with `data-dt-page-size="25"`,
      `data-dt-colfilter`, `data-dt-empty`, and `th[data-sort]`, matching the `library.html` /
      `finding.html` precedent and the attribute contract core `static/data-table.js` reads.
- [x] Added `tests/test_engagements_datatable.py`: contract test (fails on a plain table), standalone
      plain-table fallback test (all rows server-rendered, no component JS route), and a drift guard
      against redefining core `.dt-*` CSS.

## Remaining
- [ ] PR + Plague review.

## Notes / gotchas
- The template change is commit `6bec433` (already on `main`); this branch adds the tests only, so it
  does not re-wire a table that is already wired. No core CSS is shipped or redefined here.
- Standalone scribble has no `data-table.js`; mounted it is loaded by lotek's `base.html`, which is the
  whole "degrades to a plain table" contract.
