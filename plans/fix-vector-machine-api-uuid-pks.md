# Plan: fix/vector-machine-api-uuid-pks

- **Branch:** `fix/vector-machine-api-uuid-pks`  (off `main`)
- **PR:** <opened on push>
- **Status:** 🟢 ready to merge

## Purpose
Vector's model was migrated to UUIDv7 PKs (via #9/#18 — `Diagram.id`/`owner_id` are `Uuid`), and the
browser surface (`api.py`, `test_api.py`) followed. The PAT **machine** surface added in #21 was written
against the old int-keyed mental model and never caught up: `api_pat.py` still routed `<int:diagram_id>`
and `_actor_owner_id` only accepted `int`, so all 16 store/route tests in `test_machine_api.py` failed
with `AttributeError: 'int' object has no attribute 'hex'` (an int hitting the `Uuid` bind processor) —
plus a `404 == 200` where a UUID id was rejected by the `<int:>` converter. This aligns the machine
surface to UUID, mirroring the already-fixed sibling.

## Done
- [x] `api_pat.py`: routes `<int:diagram_id>` → `<uuid:diagram_id>` (4 routes) + `diagram_id: uuid.UUID`
      hints; `_actor_owner_id` now accepts `uuid.UUID` (mirrors `deps.current_actor_id`) and degrades
      loudly (warn + NULL owner) for a non-UUID id; corrected the now-stale "this package has NOT
      migrated" docstrings; `delete` returns `str(diagram_id)` (matches `api.py`).
- [x] `conftest.py`: `StubActor.id` `int = 7` → `uuid.UUID = uuid.UUID(int=7)`; docstring updated; fixed
      pre-existing stdlib import order (ruff).
- [x] `test_machine_api.py`: `StubActor(id=99…)`×2 and `FakeUser(uid=42…)` → `uuid.UUID(int=…)`; the
      `999999` "not found" sentinel → a fresh `uuid.uuid4()`; two direct `db.get(Diagram, created["id"])`
      calls wrapped in `uuid.UUID(...)` (JSON id comes back as a string); inverted the two "Integer-key
      limitation" tests to "a **non-UUID** principal id degrades loudly" (now the degradation case is a
      legacy int, not a UUID).

## Remaining
- [ ] Nothing — merge.

## Notes / gotchas
- Full vector suite: **57 passed** (34 non-machine + 23 machine; 16 of the 23 were the red set). Ruff
  clean on changed files.
- The old `_actor_owner_id` int-guard meant every **real mounted** PAT write stored `owner_id = NULL`
  (admin-visible only) — a latent tenancy bug this fix also closes: mounted PAT-created diagrams are now
  correctly owner-scoped.
- Sibling reference for the pattern: `vector/vector/api.py` + `vector/tests/test_api.py` (UUID-aligned).
