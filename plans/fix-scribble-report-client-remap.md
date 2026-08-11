# Plan: fix/scribble-report-client-remap

- **Branch:** `fix/scribble-report-client-remap` (worktree: `.claude/worktrees/clientremap`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose

Close a report-authz **IDOR**. `Engagement.client_id` is a *soft* int reference with no id-space (may be
a `scribble_clients.id` from standalone, or a host `clients.id` when mounted). The host's
`user_can_view_client` compares it blind against `Job.client_id`; in a standalone→mounted DB a
scribble-space id is misread as a host client id and can collide with a real host client an attacker owns
a job under (both small sequential ints), exposing that client's whole report. Found by the adversarial
review of lotek PR #232; #6 here wired the `can_view_client` capability but did **not** address the
id-space ambiguity.

No runtime predicate closes it (the FK already denies non-existent ids; only the collision is reachable,
and a bare int can't reveal its space). The complete fix makes `client_id` unambiguously host-space in the
data — a one-shot, idempotent, mount-time remap.

## Fix — `_remap_standalone_client_ids` at the end of `scribble/db.create_all`

Guarded so it fires only on the reachable case; **load-bearing insight:** in mounted mode Scribble writes
the HOST client table, never `scribble_clients`, so `scribble_clients` having rows ⟺ standalone history.
- STANDALONE (`client_model()` is scribble's Client) → return.
- MOUNTED + `scribble_clients` empty → return (fresh mounted; never touch valid host ids).
- MOUNTED + rows → remap each engagement whose `client_id` matches a `scribble_clients` row to the host
  client of the SAME NAME (or NULL → admin-only report, secure default), then rename `scribble_clients`
  away so the next boot's create_all recreates it empty → idempotent. (No FK dependents; rename is safe.)

Residual (documented in code): mounted engagements added BEFORE the first run could have a host-space id
that coincidentally equals a `scribble_clients` row id → falsely remapped. Bounded, one-time, absent on a
normal cutover. v2 per-engagement membership replaces this whole path.

## Done
- [ ] Plan (this).

## Remaining
- [ ] `_remap_standalone_client_ids` + create_all hook in `scribble/scribble/db.py`.
- [ ] `scribble/tests/test_scribble_client_remap.py` (red→green: collision resolves by name; unmatched→
      null; idempotent; standalone untouched).
- [ ] `uvx ruff check scribble` clean; `cd scribble && python -m pytest -q` green.
- [ ] PR (bot token) + release comment.
- [ ] Re-vendor into lotek (`scripts/stage-extension.sh`) — that overwrites the hand-edited vendored copy.

## Notes
- v1 code path (Integer `client_id`); the v2-native UUID contract does not apply to this fix.
- Never hand-edit lotek's vendored copy — this monorepo is the source; re-vendor after merge.
