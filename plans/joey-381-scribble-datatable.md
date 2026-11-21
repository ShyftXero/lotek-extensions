# Plan: joey/381-scribble-datatable

- **Branch:** `joey/381-scribble-datatable`  (worktree: `~/lotek-extensions.joey-381-scribble-datatable`, off `main`)
- **PR:** #397
- **Status:** 🟡 in progress (r1 review rework landed, awaiting re-review)

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
- [x] r1 review rework (Plague, 3 findings):
  - CSS guard now scans EVERY `*.css` under `scribble/static/` (plus the page's inline `<style>`), and
    matches all three host spellings `.data-table`, `.dt-*`, `dataTable`, so a rule appended to
    `static/scribble.css` (linked into the host page on every route) goes red. Proven by appending the
    reviewer's exact reproduction and observing the failure.
  - Widget path: replaced markup-only proof with a model of `data-table.js`'s init gates (selector
    `table[data-dt]`, `thead` row, snapshotable `tbody`, parsed paging/filter attributes), pinned by a
    test that the model rejects each early-return case. Residual documented in the module docstring: no
    JS/DOM executes in this suite (browser runs are refused on this host and the suite vendored no
    jQuery/DataTables), so a rejection from CSP or a missing lib stays out of reach.
  - NITs: dropped `table-scroll` as the sole wired-vs-unwired separator (now: no `.dt-*` chrome in the
    markup plus the eligibility model), replaced `len(names) > 25` with a page-derived
    `rows > declared page size`, and split `data-dt-empty` (widget's filtered-out message) from a real
    zero-row empty-state test.

## Remaining
- [ ] Plague re-review of #397, then merge.

## Notes / gotchas
- The template change is commit `6bec433` (already on `main`); this branch adds the tests only, so it
  does not re-wire a table that is already wired. No core CSS is shipped or redefined here.
- Standalone scribble has no `data-table.js`; mounted it is loaded by lotek's `base.html`, which is the
  whole "degrades to a plain table" contract.
