# Plan: fix/scribble-uuid-attribution

- **Branch:** `fix/scribble-uuid-attribution` (worktree: `.claude/worktrees/uuid-attrib`, off `main`)
- **PR:** not opened yet
- **Status:** 🟢 ready to merge

## Purpose

Fix a data-attribution bug: when Scribble is mounted in a host whose user/client ids are UUIDs (Lotek
v2), engagement/report creation silently dropped `owner_id` (attribution) and the `client_id` link. Two
independent int-only assumptions caused it:

- `scribble/scribble/deps.py::current_actor_id()` returned `None` for any actor id that wasn't a plain
  `int` — a v2 host's `uuid.UUID` actor id failed `isinstance(ident, int)` and silently became `None`.
- `scribble/scribble/engagement_ui.py`'s `_as_int(form.get("client_id"))` (both `engagement_new` and
  `_apply_engagement_form`) parsed a UUID-shaped form value to `None` via a bare `int()` call — a
  deliberately-selected client link silently dropped.
- Downstream, `Engagement.owner_id` / `.client_id` in `scribble/scribble/models.py` were `Integer`
  columns — even had the above two been fixed, a UUID host id could not physically be stored.

Stage A (this branch) is the ORM/attribution fix in Scribble's own repo. A follow-up re-vendors it into
lotek v2 (`scripts/stage-extension.sh`).

## Fix

- **`scribble/scribble/deps.py`**: `current_actor_id()` now accepts `isinstance(ident, (int, uuid.UUID))`;
  annotation widened to `int | uuid.UUID | None`. Fail-safe kept — a bad/foreign id shape or a raising
  hook still resolves to `None`, never a write-time crash.
- **`scribble/scribble/engagement_ui.py`**: added `_as_id()` — tries `int()`, then `uuid.UUID()`, else
  `None` — and swapped it in for `_as_int()` at both `client_id` parse sites. `_as_int` is untouched and
  still used for Scribble's own always-int PKs (assessment_type_id, template_id, group_id, order_index,
  …).
- **`scribble/scribble/models.py` + `scribble/scribble/db.py`**: `Engagement.owner_id`/`.client_id`
  widened from `Integer` to a new `SoftHostId` (`scribble.db`) — a `TypeDecorator` stored as `VARCHAR(64)`
  that reconstructs the ORIGINAL Python type on read (`int` for a digit string, `uuid.UUID` for a
  UUID-shaped one). This was the load-bearing design decision (verified empirically against SQLAlchemy
  2.0.51, see commit/PR body): a plain `String` column round-trips an int as a *string*, which (a) breaks
  every existing int-equality check silently — the exact same bug shape we're fixing — and (b) crashes
  `resolve_client()`/`client_names()`'s `session.get()`/`.in_()` against a `sqlalchemy.Uuid`-typed host PK
  (that type's bind processor expects a real `uuid.UUID` object, not a string, and raises
  `AttributeError` otherwise). `SoftHostId` avoids both: `resolve_client`/`client_names` needed **zero**
  changes.
  - No Alembic here (Scribble has no migration framework — `scribble.db.create_all` is additive-only).
    A freshly created database picks up `VARCHAR(64)` directly. A **pre-existing Postgres-backed mount**
    needs a one-time manual `ALTER TABLE scribble_engagements ALTER COLUMN owner_id/client_id TYPE
    VARCHAR(64)` before mounting under a UUID host — documented in `SoftHostId`'s docstring. SQLite is
    unaffected either way (no real column-type enforcement).
  - Collateral fix: `_remap_standalone_client_ids` (db.py, the IDOR remap from PR #8) reads `client_id`
    via raw SQL, bypassing `SoftHostId`'s type decoding — so a legacy int `client_id` now round-trips as
    text at that layer. Normalized the comparison (`int(cid)` before checking against `scribble_clients`'
    int-keyed map) so the remap still fires correctly; updated `tests/test_scribble_client_remap.py`'s
    `_client_ids()` raw-SQL test helper to coerce back to int (what any real ORM read would already give
    you) rather than asserting on SQLite's storage class.

`asset_id`/`source_finding_id` (also `Integer` soft refs to host tables) have the same latent shape —
**out of scope** for this branch; flagged for the v2 re-vendor follow-up.

`scribble/scribble/api_pat.py::_opt_int` (the machine/PAT engagement-create path) has the same class of
bug on `client_id` — **not fixed here** (different, more visible failure mode: a 400, not a silent drop;
changing a JSON API's input-validation contract felt like a separate, deliberate decision) — flagged for
follow-up.

## Done
- [x] Plan (this).
- [x] `current_actor_id()` accepts int or UUID.
- [x] `engagement_ui._as_id()` + both call sites.
- [x] `SoftHostId` type + `owner_id`/`client_id` widened; `_remap_standalone_client_ids` fixed for the
      now-TEXT storage.
- [x] Tests: `test_deps.py::test_current_actor_id_reads_hook_uuid_id`,
      `test_client_model_injection.py::test_engagement_create_links_uuid_client_id_when_mounted_host_uses_uuid_ids`,
      `test_client_model_injection.py::test_engagement_create_persists_uuid_owner_id_when_mounted_host_uses_uuid_ids`
      — all 3 proven RED against pristine `HEAD`, GREEN after the fix.
- [x] `uvx ruff check` + `pyrefly check` clean on every touched file.
- [x] Full suite (`pytest tests/`, serial): identical failure set before/after (byte-for-byte diff'd) —
      9 pre-existing `test_skill.py` failures (missing, never-committed `skill/scribble-report-refine/`
      asset dir) + 1 pre-existing order-dependent flake (`test_templating.py::
      test_preview_endpoint_requires_some_input`, confirmed to fail identically on pristine `HEAD` with
      none of this branch's changes applied). Zero regressions, zero new failures.

## Remaining
- [ ] PR (bot token) + release comment.
- [ ] Re-vendor into lotek (`scripts/stage-extension.sh`) — separate follow-up task, not this branch.

## Notes / gotchas
- The `SoftHostId` round-trip is the crux of this fix — don't simplify it back to a plain `String` column;
  see the "Fix" section above for why that silently reintroduces the same bug class in a new place.
- `api_pat.py::_opt_int` on `client_id` and `EngagementFinding.asset_id`/`.source_finding_id` are known,
  related, NOT-fixed-here gaps — see above.
- Verified empirically with throwaway scripts under `/home/shyft/tmp/sa_probe/` (not committed) before
  choosing the `SoftHostId` design, rather than assuming SQLAlchemy's bind/coercion behavior.
