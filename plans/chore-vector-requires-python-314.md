# Plan: chore/vector-requires-python-314

- **Branch:** `chore/vector-requires-python-314`  (off `main`)
- **PR:** not opened yet
- **Status:** 🟢 ready to merge

## Purpose
Make the packaging metadata honest. `vector/vector/models.py` uses `uuid.uuid7` (a Python
**3.14+** stdlib API, added to vector via reconciliation PR #18), but `vector/pyproject.toml`
still declared `requires-python = ">=3.11"`. That floor is dishonest — the package will not run
on 3.11–3.13. Bump it to `>=3.14`.

## Done
- [x] vector/pyproject.toml `requires-python` `>=3.11` → `>=3.14`.
- [x] Audited the other 3 extensions for `uuid.uuid7` in shipped source:
      - **scribble** — `scribble/scribble/enrichment.py` uses `uuid.uuid7`, floor was `>=3.11` → **bumped to `>=3.14`** in this PR.
      - **cream** — `cream/cream/db.py` uses `uuid.uuid7`, floor already `>=3.14` → no change.
      - **registrar** — `registrar/registrar/db.py` uses `uuid.uuid7`, floor already `>=3.14` → no change.
- [x] vector + scribble test suites green; ruff clean on changed files.

## Remaining
- [ ] Merge (human, via web UI). Do not self-merge.

## Notes / gotchas
- Metadata-only change; no runtime behaviour changes, so no new test was added (nothing to pin
  beyond the existing suites, which continue to pass).
