# Plan: fix/scribble-report-can-view-client

- **Branch:** `fix/scribble-report-can-view-client`  (worktree: `.claude/worktrees/canview`, off `origin/main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress — paired with the lotek-side PR that adds the host capability

## Purpose

`report_html_api._authorize_engagement_view()` asks the host for `cfg.extras["can_view_client"]` and
`abort(404)`s when the key is absent — correct, since this module holds no policy to fall back to. But
**no host ever provided the key**, so every *mounted* report 404'd for every actor, admin included.

This half fixes the **test harness**, which was hiding it. `tests/conftest.py::_wire_stub_host` mirrors
lotek's `_inject_host` and had the same hole, so `test_scribble_report_authz.py` ran **3 failed / 4
passed** — and the three failures were exactly the ALLOW cases while the four passes were deny cases that
a dead route satisfies for free.

The host half lands in lotek as `app/access.py::user_can_view_client` + `HostServices.can_view_client`.

## Done

- [x] `StubHost.can_view_client(client_id, actor)` + `viewable_client_ids`, wired into
      `_wire_stub_host` as `cfg.extras["can_view_client"]`. Mirrors the host's real rule: admin reads any
      client, a non-admin reads a client it holds, `client_id IS NULL` is admin-only.
- [x] `test_scribble_report_authz.py` rewritten onto the **client** axis. **11 passed** (was 3 failed / 4
      passed).
- [x] New ALLOW coverage: operator-with-grant reads the report *and* the export; a grant covers every
      engagement under that client.
- [x] New DENY coverage: a grant on a *different* client does not carry.
- [x] `test_a_host_missing_the_capability_fails_closed` — deletes the extras key and asserts 404, so if
      the host ever stops injecting it, one test says *why* every report went dark.
- [x] `uvx ruff check` clean.

## Remaining

- [ ] Land the lotek-side PR (host capability) — this branch's stub is only a faithful *model* of it.
- [ ] Re-vendor into lotek after both merge, so `extensions/scribble/` carries the updated tests.

## The semantic change, stated plainly

**The authorization axis moved from engagement-owner to client, and these tests moved with it.**

The old assertions encoded *"admins see everything; a non-admin sees only engagements it OWNS; a NULL
owner is admin-only"* — a hand-copy of lotek's `user_can_view_job`. That copy is what the refactor
removed, because it had **inverted** relative to per-engagement membership (it granted every admin a full
read plus the creator a read on a client it may hold no membership under).

Two consequences, each now asserted rather than implied:

1. **A grant is per-client**, so it covers every engagement under that client. That is looser than
   per-engagement and is the granularity the caller asks about — `can_view_client` takes a client id.
   `test_a_grant_covers_every_engagement_under_that_client` records it as a decision.
2. **An engagement with `client_id IS NULL` is admin-only** — *stricter* than the old owner rule, which
   let the owning operator read it. Nothing to attribute a read to means the secure default applies.
   Worth knowing before the launch: a demo engagement created without a client is admin-only.

`Engagement.owner_id` stays ATTRIBUTION and must not be reintroduced as an authorization key.

## Notes / gotchas

- **The stub must not be kinder than the host.** It would have been easy to give `StubHost` owner-based
  logic so the old tests passed untouched — that is exactly the harness-kinder-than-production trap, and
  it would have re-hidden the bug. The stub holds a `set` of client ids only because it has no jobs table
  to derive them from; the *shape* of the answer is production's.
- The host's real rule derives client visibility from **job ownership** (a non-admin reads a client it
  owns a job under), following `scope_assets_query`'s existing precedent rather than inventing an axis.
  v1 has no membership table — `models.py` says of `Asset.client_id` outright: *"a dedupe/attribution
  key, NOT an authorization key — User has no client_id"*. v2's per-engagement membership replaces it.
- `test_standalone_no_host_applies_no_authorization` is unchanged and still passes: no host bundle means
  no host authorization model, so the guard returns early.
