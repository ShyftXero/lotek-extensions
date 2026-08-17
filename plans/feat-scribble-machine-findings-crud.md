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
- [x] **#47** — `api_pat._client_not_found()`: one static next-step hint, appended unconditionally so
      the "no such client" and "exists but you hold no grant" refusals stay byte-identical. Verified
      against core `origin/main` (`d31e424`) before writing the message: `POST /api/v1/engagements`
      exists, is `_require_admin_or_404` (so a non-admin gets 404, not 403), and self-grants the creator
      an `operator` membership — the hint says the admin-only part too, because a hint that named only
      the route would dead-end a non-admin one step later. 5 tests in
      `tests/test_machine_client_onboarding.py`; `docs/SCRIBBLE.md` refusal-code row updated.

## Remaining

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

Verbatim pytest output of each new guard being broken, watched fail, and restored. Nothing is recorded
here that was not actually run.

### #47 · guard 1 — the client 404 must name the next step

`tests/test_machine_client_onboarding.py::test_client_not_found_names_the_next_step`

Broke it by reverting `_client_not_found`'s detail to the old bare string:

```
--- RED (hint removed) ---
        assert body["error"] == "not_found"
        detail = body["detail"]
>       assert "record-only" in detail
E       AssertionError: assert 'record-only' in 'client not found'

tests/test_machine_client_onboarding.py:73: AssertionError
=========================== short test summary info ============================
FAILED tests/test_machine_client_onboarding.py::test_client_not_found_names_the_next_step - AssertionError: assert 'record-only' in 'client not found'
1 failed, 4 passed in 1.99s
```

```
--- GREEN (hint restored) ---
5 passed in 2.41s
```

### #47 · guard 2 — adding the hint must NOT create an existence oracle

`tests/test_machine_client_onboarding.py::test_client_refusal_is_byte_identical_for_missing_and_ungranted`

Broke it with the "helpful" version of the same fix — look the client row up and say which of the two
cases the caller hit:

```
--- RED (refusal reveals existence) ---
        assert ungranted.status_code == missing.status_code == 404
>       assert ungranted.data == missing.data, (
E       AssertionError: the two client refusals differ — that is an existence oracle over the client id
E       space: b'{"client_exists":true,"detail":"client not found, or you hold no membership under it. …
E       vs b'{"client_exists":false,"detail":"client not found, or you hold no membership under it. …
E       At index 17 diff: b't' != b'f'
tests/test_machine_client_onboarding.py:92: AssertionError
=========================== short test summary info ============================
FAILED tests/test_machine_client_onboarding.py::test_client_refusal_is_byte_identical_for_missing_and_ungranted
1 failed, 4 passed in 2.28s
```

```
--- GREEN (oracle removed) ---
5 passed in 1.86s
```

Note what made this red possible: the fixture gives `UNGRANTED` a REAL `scribble_clients` row and
`NONEXISTENT` none, so the two requests genuinely differ in the world. Without that row the assertion
would hold for a route that leaked the difference, i.e. it would be a test of nothing.
<!-- Lifecycle: create + commit this FIRST thing when cutting the branch; keep it current; KEEP it —
     it merges to main with the branch as a durable record (since 2026-07-28; see CLAUDE.md
     "Per-branch plan"). -->
