# Plan: feat/cream-list-endpoints-bound

- **Branch:** `feat/cream-list-endpoints-bound` (off `main`)
- **Status:** 🟡 code + tests green locally — not yet PR'd
- **Issues:** follow-up to the cream UI-maturity PR (#207) — the review's CONCERN 2

## Purpose

The dashboard PR (#207) capped the HTML list but left its two JSON siblings on the old pattern: both
`GET /cream/api/documents` (cookie) and `GET /cream/machine/documents` (PAT) did
`select(Document)...all()` with a **post-query Python** visibility filter and **no bound** — the exact
unbounded, DoS-shaped read #207 replaced on the HTML surface, and the one the dashboard's own truncation
notice points users at. Root-cause fix: a single scoped+bounded query helper all three route through.

## Done

- `service.scoped_documents(db, vis, *, status, kind, limit, offset)` — the one scoped+ordered query, with
  read-scope (`vis` empty → zero rows, fail-closed), optional status/kind, SQL limit/offset, and an `id`
  tiebreaker so offset paging is stable across `created_at` ties. Plus `clamp_limit` (default 200, max
  500) and `clamp_offset` (≥0).
- Both JSON endpoints now `?limit`/`?offset` page and return `{documents, limit, offset, has_more}`
  (fetch `limit+1`, slice, report `has_more`). `documents` key preserved — additive to the contract.
- `blueprint.dashboard` refactored to call `scoped_documents` too (all three surfaces on one seam).
  Dropped the now-unused `select` imports from `blueprint.py` / `api_pat.py`.
- Tests: `test_list_pagination.py` (bound + `has_more`, disjoint offset pages, limit/offset clamping,
  default, machine endpoint paged). Full cream suite green (187); ruff + pyrefly clean.

## Remaining

- PR + merge. Re-pin into core with the other cream change when core next re-pins.

## Notes / gotchas

- **Contract change:** the JSON list is now capped at a default 200 per page. A consumer that assumed one
  unbounded response now gets the first page + `has_more=true`; page with `?offset=`. Documented in both
  endpoint docstrings and the PR body. This is the DoS fix — the alternative (silent hard cap) would drop
  rows with no signal.
- Machine-endpoint read-scope is already covered by `test_reads_are_scoped_to_the_tokens_visible_engagements`;
  this branch adds the paging behaviour, not the scoping.

## Evals

- **Hypothesis:** all three list surfaces are read-scoped + bounded through one helper; the JSON endpoints
  page correctly (no overlap, honest `has_more`) with the `documents` contract preserved. **Mode:** 2.
- **Graders:** cream's own suite; the 5 new pagination tests + the existing scope/list tests.
- **Baseline:** before, `grep -n 'select(Document)' api.py api_pat.py blueprint.py` = three forked
  unbounded reads; after, one `service.scoped_documents` caller each.
