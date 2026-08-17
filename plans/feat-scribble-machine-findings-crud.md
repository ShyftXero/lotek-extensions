# Plan: feat/scribble-machine-findings-crud

- **Branch:** `feat/scribble-machine-findings-crud`  (worktree: `.claude/worktrees/ux-findings-crud`, off `main`)
- **PR:** not opened yet (the orchestrator opens it)
- **Status:** 🟡 in progress

## Purpose

Two client-reported defects from the TeamsPlus deliverable punch list, both in scribble's PAT machine
API (`scribble/api_pat.py`):

- **ext#41 — the machine API can CREATE findings and nothing else.** 13 routes, and the only findings
  route is `POST /engagements/<id>/findings`. No read-back, no list, no edit/delete/move/group. So an
  agent that authors a report over a PAT cannot fix a title, cannot group or reorder, and cannot even
  see what it created (`GET /engagements/<id>` returns a bare `finding_count`). The cookie UI has all
  of it and the model already carries `group_id`/`order_index`/`parent_id`/`FindingGroup.order_index`/
  `order_mode` — this is a missing surface, not a data-model change.
- **ext#47 — `client not found` dead-ends the caller.** `POST /api/v1/clients` → 201, then
  `POST /scribble/machine/engagements` → 404 `client not found`, because a core client is record-only
  until `POST /api/v1/engagements` self-grants the creator an operator membership. The RULE is correct
  (`can_view_client_id` is membership-only on purpose — admitting the owner axis is a cross-tenant
  escalation). Only the message is wrong.

## Done

- [x] Plan file committed first.

## Remaining

- [ ] **#47** — a static next-step hint on the client 404, appended unconditionally so "no such client"
      and "exists but you hold no grant" stay byte-identical. Its own commit.
- [ ] **#41** — extract the board mutation algorithms out of `engagement_ui.py` into
      `scribble/findings_service.py` so both surfaces run the same code.
- [ ] **#41** — 10 new `machine_bp` routes (13 → 23).
- [ ] Docs: `docs/SCRIBBLE.md`'s machine-API table (stale — it says "Nine routes").
- [ ] Red-then-green transcript per new guard, captured below verbatim.

## The intended surface (10 routes)

| Method · path | Scope |
|---|---|
| `GET /engagements/<eid>/findings` | read |
| `GET /findings/<fid>` | read |
| `PATCH /findings/<fid>` | write |
| `DELETE /findings/<fid>` | write |
| `POST /findings/<fid>/move` | write |
| `POST /engagements/<eid>/findings/move` (bulk) | write |
| `POST /engagements/<eid>/groups` | write |
| `PATCH /engagements/<eid>/groups/<gid>` | write |
| `DELETE /engagements/<eid>/groups/<gid>` | write |
| `POST /engagements/<eid>/groups/reorder` | write |

## Notes / gotchas

(filled in as the work lands)

## Red-then-green

Not captured yet — this section is filled with the VERBATIM pytest output of each new guard being
broken, watched fail, and restored. Nothing goes here that was not actually run.
<!-- Lifecycle: create + commit this FIRST thing when cutting the branch; keep it current; KEEP it —
     it merges to main with the branch as a durable record (since 2026-07-28; see CLAUDE.md
     "Per-branch plan"). -->
</content>
