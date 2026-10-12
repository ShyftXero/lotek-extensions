# Plan: feat/121-chore-scribble-drop-the-deprecated-diagr

- **Branch:** `feat/121-chore-scribble-drop-the-deprecated-diagr`  (worktree: `.wt-121`, off `main`)
- **PR:** not opened yet
- **Status:** 🟢 ready to merge

## Purpose
Drop the deprecated `diagrams` alias from the `GET /scribble/machine/engagements/<id>/attack-paths`
response. The alias was kept for one release (#116); #121 is the tracking issue for its removal.

## Done
- [x] Removed `"diagrams": out` from `api_pat.py` response builder
- [x] Removed `diagrams` entry from `openapi.py` schema
- [x] Updated `test_machine_openapi.py` to assert `diagrams` is ABSENT (locks removal in)
- [x] Updated `test_report_attack_path.py` to read `attack_paths` instead of `diagrams`
- [x] Updated `docs/SCRIBBLE.md` to reflect removal

## Remaining
- nothing

## Notes / gotchas
- `diagrams` as a Python attribute on the ORM `ReportBoard.diagrams` relationship is unrelated — it's the
  relationship name, not the API key. Those references are correct and untouched.
- Vector extension uses `"diagrams"` as its OWN response key (different route/resource) — not affected.
