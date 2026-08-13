# Plan: feat/scribble-visible-client-ids

- **Branch:** `feat/scribble-visible-client-ids`  (worktree: `.claude/worktrees/visible-clients`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose

Consume the host's new `visible_client_ids` seam so Scribble's two list routes scope **in SQL** instead of
reading every engagement row and filtering in Python.

This is the follow-up ext#12 recorded as an accepted cost. That branch closed the unscoped-list hole with
the only tool the seam offered — a per-client PREDICATE (`can_view_client`) — which forced
`blueprint.dashboard` to drop its `LIMIT 10` and read the whole table to filter and count. Correct, but it
trades a tenancy bug for a scan. lotek's companion PR adds the SET form; this uses it.

## What changed

- `scribble/authz.py`:
  - `host_visible_client_ids()` — reads `extras['visible_client_ids']`, returning `None` when there is no
    set available (standalone, or a host bundle predating the hook). **`None` and `frozenset()` are
    deliberately different answers**: the first means "fall back to the predicate", the second means "this
    actor holds nothing". A truthiness check that collapsed them would be a fail-open.
  - `visible_engagements(db, stmt, actor)` — the one entry point both list routes call. SQL path
    (`WHERE client_id IN (…)`) when the set exists; the existing `filter_visible_engagements` predicate
    path when it doesn't, so an older host bundle keeps its scoping rather than silently losing it.
- `scribble/blueprint.py::dashboard` and `scribble/engagement_ui.py::engagements` now call it. The
  dashboard's stat tiles still derive from the same visible set, so tiles and list cannot disagree.

## Done

- [x] `host_visible_client_ids` + `visible_engagements`, both list routes switched over.
- [x] `tests/test_visible_client_ids_scoping.py` (7): the hook's `None`-vs-empty distinction, the SQL path,
      the empty-set fail-open trap, **both paths agree** (the SQL path must not become a second, drifting
      copy of the predicate), and the two routes end to end with the hook wired.
- [x] Red→green: scoping dropped from `visible_engagements` → **4 fail**; restored → **19 pass** together
      with the existing `test_scribble_list_tenancy.py`.
- [x] `uvx ruff check scribble tests` clean.

## Remaining

- [ ] Full scribble suite.
- [ ] PR.
- [ ] After merge: re-vendor into lotek (`scripts/stage-extension.sh`). Order does not matter for
      correctness — the consumer degrades to the predicate when the host lacks the hook, and the host hook
      is inert until something reads it — but prod only gets the SQL path once both have landed and a
      release tag is cut.

## Notes / gotchas

- Named `host_visible_client_ids`, matching cream's `host_visible_engagement_ids` convention and keeping
  the `host_` prefix so it cannot be confused with (or trip the boundary guard on) the seam function name
  itself. This module holds no policy — it asks.
- **This is client-level scoping, which is coarser than core's engagement-level unit**, and that is
  inherited, not introduced: Scribble's `Engagement` carries only a soft `client_id` and no reference to a
  core engagement, which is exactly why `can_view_client` exists and is documented as a known gap in
  `host_contract.make_can_view_client`. The set is that predicate's plural, nothing wider. The real fix
  remains "Scribble's engagement carries the core engagement id", at which point both collapse into the
  ordinary engagement check.
