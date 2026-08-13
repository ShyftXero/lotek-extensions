# Plan: fix/scribble-pat-authz-uuid

- **Branch:** `fix/scribble-pat-authz-uuid`  (worktree: `.claude/worktrees/pat-authz-uuid`, off `main`)
- **PR:** https://github.com/ShyftXero/lotek-extensions/pull/13
- **Status:** 🟢 ready to merge

## Purpose

Close the remaining Scribble authz/UUID follow-ups flagged out-of-scope by #9/#11 (and revisited after
#12 landed the bulk of the machine-blueprint tenancy work). Three items were in scope:

1. `machine_bp` (`scribble/api_pat.py`) engagement-scoped routes with no tenancy check.
2. `_opt_int` applied to a host id (`client_id`) instead of a tolerant int-or-UUID parse.
3. Existence-oracle inconsistency: `create_artifact`/`templating_preview` returned a JSON 404 for
   "engagement not found" but a bare `abort(404)` (Flask's default HTML page) for "forbidden".

## What was found

**Items 1 and 2 were ALREADY CLOSED by #12** (`fix/scribble-machine-tenancy`, merged same day as this
branch was cut, just before it): `scribble_add_finding` and `scribble_promote_job` both call
`can_view_engagement(engagement, host.actor())` before touching the engagement, and `client_id` in
`scribble_create_engagement` is parsed by `_opt_host_id` (int OR UUID), not `_opt_int`. Verified by
reading the merged code and `tests/test_scribble_machine_tenancy.py` (14 tests, incl.
`test_create_engagement_accepts_a_uuid_client_id`). No other `machine_bp` route touches engagement/client
data (`list_templates`/`get_template`/`create_vuln_map`/`list_vuln_map`/`resolve_template` are all
library-wide tables with no engagement axis; `lotek_finding_id`/`template_id`/`group_id` are correctly
`_opt_int` — they're Scribble's own int PKs or lotek's still-int `Finding.id`, not host ids).

So this branch's real work is **item 3 only**.

## Done

- [x] `scribble/artifacts_api.py::create_artifact` and `scribble/templating_api.py::templating_preview`:
      replaced the direct `authorize_engagement_view(engagement)` call (which `abort(404)`s -- Flask's
      default HTML error page) with an inline `can_view_engagement(engagement, current_actor())` check
      folded into the SAME `if engagement is None or not …: return jsonify(...), 404` branch already
      used for "engagement not found". Both refusals now return this route's own JSON shape; a
      forbidden-but-real engagement id is no longer distinguishable from a nonexistent one by
      content-type or body shape.
- [x] Updated `scribble/authz.py`'s module docstring (the "two known gaps" section + the "plain
      predicates" list) and the two routes' own inline comments to describe `can_view_engagement`
      instead of `authorize_engagement_view`, and the corresponding comments in
      `tests/test_scribble_tenancy_gate.py`.
- [x] Two new tests in `tests/test_scribble_tenancy_gate.py`:
      `test_artifact_upload_not_found_and_forbidden_are_byte_identical` (the artifact route's refusal
      names no id, so both cases must be byte-for-byte the same response) and
      `test_templating_preview_not_found_and_forbidden_share_the_same_shape` (the preview route's
      refusal echoes the caller-supplied id, which isn't a leak since the caller already knows it it
      chose — so this asserts same status/content-type/JSON-shape instead of byte equality).
- [x] Red -> green proven by hand (not wired into CI as a toggle): reverted `artifacts_api.py`,
      `templating_api.py`, `authz.py` to the pre-fix `HEAD` versions, ran the two new tests -- both
      FAILED (`assert 'text/html; charset=utf-8' == 'application/json'`), restored the fix -- full
      `test_scribble_tenancy_gate.py` (16 tests) passed.
- [x] `uv run ruff check scribble tests` clean. `uv run pyrefly check` clean on every changed file.
- [x] Full scribble suite: see run results below.

## Remaining

- [ ] Re-vendor into lotek (`scripts/stage-extension.sh`) -- after merge, per the standing convention
      (not part of this fix; no lotek-side behavior changed until the snapshot is restaged).

## Notes / gotchas

- The task brief assumed items 1/2 were still open (written against #9/#11 before #12 landed). Verified
  independently by reading `api_pat.py` and its test file rather than trusting the brief -- worth a
  second look if a future follow-up brief references pre-#12 state again.
- `templating_preview`'s not-found/forbidden message embeds the caller-supplied `engagement_id`
  (`f"engagement {engagement_id} not found"`), so the two branches are same-SHAPE, not byte-identical --
  documented in the test itself so a future reader doesn't "fix" it into a stricter assertion that can't
  actually pass (the id in the message is attacker-supplied input, not new information).
- Pre-existing, unrelated: `tests/test_skill.py` fails on this repo (no `skill/` directory here) -- do
  not treat as a regression.
