# Plan: docs/deep-dive

- **Branch:** `docs/deep-dive`  (off `main`)
- **PR:** not opened yet (paired with lotek `feat/docs-deep-dive`)
- **Status:** 🟡 in progress

## Purpose
Documentation deep-dive across the four bundled extensions, paired with lotek's `feat/docs-deep-dive`
(which makes bundled extensions mount by default and renders each extension's operator doc **from its own
installed package** instead of from the lotek checkout). This branch:

1. Adds each extension's **operator doc** at `<pkg>/docs/<NAME>.md`, verified against the actual source
   (routes, models, machine API, seed) — moving the detailed content OUT of lotek core, where it no
   longer belongs.
2. **Packages** the doc into the wheel (`[tool.hatch.build.targets.wheel.force-include]`) and points the
   manifest's `[host] docs` / `docs_title` at it, so lotek reads it via `importlib.resources`.
3. Refreshes each extension `README.md` + the top-level `README.md` — the retired `stage-extension.sh`
   vendoring flow is replaced with the pinned-git-dep / entry-point reality, and the default-on posture.

## Done
- [x] scribble/docs/SCRIBBLE.md, vector/docs/VECTOR.md, cream/docs/CREAM.md, registrar/docs/REGISTRAR.md
      — authored + packaged (`force-include` + `[host] docs`), verified against source by the workflow.
- [x] Per-extension READMEs refreshed; top-level README de-vendored + default-on noted.

## Remaining
- [ ] ruff + each extension's tests green; commit; open PR into `main`.

## Notes / gotchas
- Doc paths: source `<pkg>/docs/<NAME>.md`; `force-include "docs/<NAME>.md" = "<pkg>/docs/<NAME>.md"`
  ships it at the wheel package root so `importlib.resources.files("<pkg>")/docs/<NAME>.md` resolves it.
- Extension docs are **image-free / absolute-link-only** (they render from a wheel with no per-package
  image route, and on GitHub).
- **Known follow-up (not this branch):** scribble's `host.require_scope` delegates to the host gate at
  request time but does not stamp `SCOPE_ATTR`, so lotek's introspective OpenAPI / `INV-INPUT-04`
  (`test_every_machine_route_is_scope_gated`) can't see the scope on scribble's machine routes — the
  pending #288 monorepo port. Pre-existing; fixing it + bumping lotek's pin is a separate change.
- After this merges, bump lotek's pins (`uv lock --upgrade-package <ext>`) so the in-app Docs page renders
  the new docs from the installed wheels.
