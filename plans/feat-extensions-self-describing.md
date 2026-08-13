# Plan: feat/extensions-self-describing

- **Branch:** `feat/extensions-self-describing`  (off `main`)
- **PR:** opened into `main` (see release comment)
- **Status:** 🟢 ready to merge

## Purpose
Make the 4 extensions (`scribble`, `vector`, `cream`, `registrar`) self-describing and
pip-discoverable so lotek can consume them as INSTALLED packages (pinned git deps) instead of
vendoring their source. Purely additive — the current vendoring path (`stage-extension.sh`) must keep
working unchanged.

## What each extension gains (additive)
1. A new top-level `[mount]` table in `lotek-extension.toml` carrying the host mount metadata that
   lotek's `stage-extension.sh` currently GENERATES into its own `[extension]` table
   (`name`/`entrypoint`/`url_prefix`/`seed`). Named `[mount]`, NOT `[extension]`, so there is no
   TOML duplicate-table clash while both the generated file and this file coexist during the
   vendoring transition.
2. A `[project.entry-points."lotek.extensions"]` row in `pyproject.toml` — the pip-discovery signal.
3. A hatch `force-include` so `lotek-extension.toml` ships INSIDE the wheel's package dir, readable
   post-install via `importlib.resources.files("<pkg>").joinpath("lotek-extension.toml")`.

## Mount values baked in (recovered from lotek's generated extension.toml manifests)
| ext       | name      | entrypoint | url_prefix  | seed                          |
|-----------|-----------|------------|-------------|-------------------------------|
| scribble  | scribble  | scribble   | /scribble   | scribble.seed:seed_defaults   |
| vector    | vector    | vector     | /vector     | vector.seed:seed_defaults     |
| cream     | cream     | cream      | /cream      | cream.seed:seed_defaults      |
| registrar | registrar | registrar  | /registrar  | (none — key omitted)          |

## Done
- [x] `[mount]` table + entry-point + force-include for all 4 extensions
- [x] scratch-venv install (real wheels, non-editable) proves both discovery signals for all 4:
      `entry_points(group="lotek.extensions")` -> `['cream','registrar','scribble','vector']`;
      `importlib.resources.files("<pkg>").joinpath("lotek-extension.toml")` resolves for all 4.
- [x] each extension's own pytest suite green — vector 34, cream 112, scribble 520 (of 529; the 9
      `test_skill.py` failures are PRE-EXISTING: they assert a `skill/scribble-report-refine/` dir that
      is untracked on origin/main, so they fail on pristine main too). registrar has no own tests dir.
- [x] confirmed no `[[nav]]`/`[capabilities]`/`[host]`/`[db]` content changed (diff = 108 insertions,
      0 deletions; tomllib confirms every table preserved).
- [x] vendoring path proven intact: simulated stage-extension.sh's `[extension]`+appended-manifest
      concatenation for all 4 — parses clean, both tables coexist, `[mount]` == `[extension]` values.

## Remaining
- [ ] human merges the PR (monorepo main is REVIEW_REQUIRED)

## Notes / gotchas
- Registrar has NO seed — the `seed` key is omitted entirely from its `[mount]`, matching the
  generated manifest which had no `seed`.
- The entry-point VALUE is the entrypoint MODULE (e.g. `scribble = "scribble"`); `ep.load()` imports
  the module exposing `register(...)`, `ep.name` is the slug.
- Vendoring path untouched: `stage-extension.sh` emits `[extension]` then appends this file; since we
  add `[mount]` (a different table name), tomllib sees no duplicate table.
