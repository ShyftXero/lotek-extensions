# Plan: fix/reconcile-vendored-drift

- **Branch:** `fix/reconcile-vendored-drift`  (worktree: `.claude/worktrees/reconcile-drift`, off `main`)
- **PR:** https://github.com/ShyftXero/lotek-extensions/pull/18
- **Status:** 🟢 ready to merge

## Purpose
lotek vendors these four extensions in-tree and is about to switch to consuming them as **git deps**
straight from this monorepo. Any fix that was applied to lotek's *vendored* copy (hand-edited in-tree,
never pushed here) would be **silently reverted** by that switch. This branch reconciles the one class of
stranded fix that exists — the **UUIDv7 alignment of `vector`** — back into the monorepo so nothing is
lost on the cutover.

Vector's models/routes/actor-id still used **Integer** PKs and `int`-guarded actor ids here, which also
**violates this repo's own documented v2-native contract** (CLAUDE.md: "UUIDv7 surrogate PKs; cross-core
refs are `sqlalchemy.Uuid`, never Integer/String"). lotek's core keys `User`/`Client`/`Job` on UUIDv7, so
the old `isinstance(ident, int)` guard in `current_actor_id()` returned `None` for every mounted diagram —
a silent owner-scope loss (`owner_id == None` reads as universally visible). This is the security-relevant
core of the port.

## Done
- [x] Compared all 4 extensions' lotek-vendored `origin/main` packages against this monorepo's packages.
- [x] **vector:** ported the 4 vendored-ahead UUIDv7 files — `deps.py`, `api.py`, `blueprint.py`,
      `models.py` (Integer→`Uuid`/`uuid.UUID`, `<int:>`→`<uuid:>` route converters, `default=uuid.uuid7`,
      loud-degrade actor-id guard). Byte-verified against lotek `origin/main`.
- [x] Left the 3 **monorepo-ahead** vector files untouched: `seed.py` (#14 RFC5737 TEST-NET IPs — the
      vendored copy still had the OLD real routable IPs; porting it would REVERT #14), `db.py` +
      `standalone.py` (only a stale `Fraction`→`Scribble` docstring the rename already fixed here).
- [x] **scribble:** clean — vendored package is byte-identical to the monorepo (re-vendored from #13);
      monorepo is even further ahead via #17. Nothing stranded.
- [x] **cream:** monorepo-ahead — vendored copy predates the #5 deliverable-engine feature (float money,
      no Brand/NumberCounter/scope). Nothing stranded.
- [x] **registrar:** monorepo-ahead — only a stale `fraction`→`scribble` docstring word. Nothing stranded.

## Remaining
- [ ] Human review + squash-merge. Do not self-merge.

## Notes / gotchas
- This is a 3-way reconciliation, NOT a blind vendored→monorepo overwrite. The vendored snapshot was
  staged from an OLDER monorepo commit, so on some files the monorepo is newer. Each file was judged
  individually: port only where the *vendored* copy carries a fix the monorepo lacks.
- `models.py` uses `uuid.uuid7`, which is **Python 3.14+ stdlib**. lotek runs 3.14; lotek's OWN vendored
  vector `pyproject.toml` keeps `requires-python = ">=3.11"` despite this, so this port matches lotek
  exactly and does not touch `pyproject.toml` (a `requires-python` bump to `>=3.14` would be a coherent
  follow-up but is out of scope for a drift-reconciliation).
- Verified: `ruff check vector` clean; `vector` test suite green on 3.14 (the 2 UUID-dependent tests that
  a UUIDv7-less monorepo failed now pass).
