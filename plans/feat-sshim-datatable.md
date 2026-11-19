# Plan: feat/sshim-datatable

- **Branch:** `feat/sshim-datatable` (worktree: `~/lotek-extensions.feat-sshim-datatable`, off `origin/rc/2026-11-18`)
- **PR:** not opened yet — targets `rc/2026-11-18`
- **Issue:** ShyftXero/lotek-extensions#379
- **Status:** 🟡 in progress

## Purpose
The running-pivots list is a bare `<table>`: no sort/filter/paging, no way to act on more than one
pivot. Wire it to the core data-table contract and add id-keyed multi-select + a bulk kill that
reports a per-id result.

## Done
- [x] Claimed #379 (`status:doing`, `feat/sshim-datatable`).

## Remaining
- [ ] `plan.status_rank` — one home for the lifecycle sort order (active → expired → killed).
- [ ] Service `kill_many` — per-id `load_visible` (404-not-403), per-pivot audit, `give_back_relay`,
      per-id result. Both surfaces call it.
- [ ] Browser: `data-dt` table + local macros emitting the documented attribute contract, checkbox
      column, hidden-selection-safe selection JS, server-rendered confirmation, result page.
- [ ] PAT twin: `POST /machine/sessions/bulk-kill`.
- [ ] Tests: two ids killed + both released; invalid id mixed in; hidden/aged-out selection; audit per
      pivot; markup/JS drift guards; AST no-`app.*` green.
- [ ] `uv run --extra dev pytest -q` + `uvx ruff@0.17.5 check sshim tests`.

## Notes / gotchas
- **Component route:** no host template seam exists (core's `_data_table.html` is not resolvable from a
  standalone sshim app and sshim may not import `app.*`), so sshim mirrors the documented attribute
  contract in its own `sshim/_macros.html` — the same route exploiteer took. Core's `data-table.js`
  activates the table when mounted; standalone it degrades to a plain table.
- Selection is a `Set` of session ids keyed by checkbox value, not DOM state: DataTables detaches/
  hides rows on filter/page, so the selection must be independent of what is on screen. "Select all"
  only touches the currently visible rows; hidden selections are never cleared. The form submits one
  hidden `ids` input per selected id, so a filtered-out selection still reaches the server.
- Confirmation is SERVER-rendered (`confirm=1` gate, mirroring the single-pivot kill), so it works with
  JS off and lists profile + rendezvous + port. Audit stays per pivot inside `kill_session`.
