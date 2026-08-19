# Plan: fix/scribble-core-engagement-uuid-map

- **Branch:** `fix/scribble-core-engagement-uuid-map`  (worktree: `.claude/worktrees/e-uuidmap`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose
Issue #49: `scribble_engagements` has no column pointing at the core engagement UUID, and the
5 engagement-scoped machine routes use `<int:engagement_id>`, which 404s a UUID at the routing
layer before the handler ever runs. A PAT caller holding the core engagement UUID has no way to
address the matching scribble engagement or discover the mapping. This branch adds a nullable
soft-ref column (`core_engagement_id`), accepts either id space on the addressing path via a
resolver helper, and surfaces the mapping in list/get output. Explicitly does NOT block on #36
(UUIDv7 PK migration, `feat/scribble-alembic-uuid-pks`) — this is the decision-independent
addressing slice the issue splits out.

## Done
- [x] Plan file committed first
- [ ] models.py: add `core_engagement_id` column (SoftHostId, nullable, index=True)
- [ ] api_pat.py: `_resolve_engagement` helper (int-or-UUID lookup + tenancy check)
- [ ] api_pat.py: create route accepts + echoes `core_engagement_id`
- [ ] api_pat.py: `_engagement_summary` surfaces `core_engagement_id`
- [ ] api_pat.py: widen 5 engagement-scoped routes from `<int:engagement_id>` to `<engagement_id>`
- [ ] api_schemas.py: `CreateEngagementRequest.core_engagement_id`
- [ ] tests: red-then-green for create/address/list/404/tenancy
- [ ] SCRIBBLE_ID_IDENTITY.md updated
- [ ] Gates (ruff/pyrefly/pytest/adversarial/review/transcripts) + PR

## Remaining
- Implement steps above; run fast-tier checks; open PR closing #49.

## Notes / gotchas
- index=True NOT unique=True — create_all's additive path can retrofit an index but not a UNIQUE
  constraint on already-populated DBs.
- SoftHostId stores as TEXT via str(value); assert on the UUID object in tests, not a string.
- After `_resolve_engagement`, rebind `engagement_id = engagement.id` so downstream code (group
  comparisons, audit subject_id, promote ref_id) needs no further edits.
- Do not touch #36 / the alembic UUIDv7 PK branch.
