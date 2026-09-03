# Plan: feat/kit-skeleton

- **Branch:** `feat/kit-skeleton`  (worktree: `.claude/worktrees/attackpath-kit`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose

Create `kit/` — `lotek-kit`, a **shared contract library** that is deliberately **not an extension**.
It exists because lotek core and Scribble both need the same two things and neither may own them:
the `attackpath/v1` document schema, and (in the follow-on PR) one reorder implementation instead of
the three native-HTML5 drag copies the repo carries today.

Resolves the structural half of #149, under map #148. This PR is the skeleton and the schema only;
the browser assets and the Flask asset blueprint land in `feat/kit-reorder-assets`, stacked on this.

## Why the design is shaped this way

Both facts below were measured, not assumed, and both eliminate the obvious alternatives:

- **`ShyftXero/lotek` is PRIVATE; `ShyftXero/lotek-extensions` is PUBLIC.** A shared package published
  out of core's repo would put a GitHub token into every public extension's `uv.lock`. "Inside core but
  independently installable" is dead on mechanics.
- **A hand-bumped semver has no machinery behind it here.** All six extension subprojects are
  `version = "0.1.0.dev0"`, unmoved since inception; the real version is derived from the git build id
  (`scripts/build_id.py`, `.github/workflows/release-tag.yml`). So `kit/` uses `dynamic = ["version"]`
  and a hatchling metadata hook that emits the same dated string. Nothing to hand-bump, nothing to
  drift.

## Done
- [x] `plans/feat-kit-skeleton.md` (this file), committed first
- [x] `kit/pyproject.toml` — `lotek-kit`, `dynamic = ["version"]`, `dependencies = []`
- [x] `kit/hatch_build.py` — dated build-id metadata hook
- [x] `kit/lotek_kit/__init__.py` — `__version__` only; deliberately no `require()`
- [x] `kit/lotek_kit/attackpath.py` — port of `vector/vector/schema.py`
- [x] `kit/lotek_kit/assets.py` — stdlib asset accessor
- [x] `kit/tests/` — 44 tests, all green
- [x] `.claude/hooks/rails_gate.py` `_SUBPROJECTS` += `"kit"`
- [x] `CLAUDE.md` + `kit/README.md`: `kit/` is not an extension
- [x] Verified end to end: `uv sync` installs `lotek-kit==2026.9.3.40038+ga467191` (the dated build id
      with HEAD's short hash), `uv build --wheel` ships `lotek_kit/static/` as package data with no
      `force-include`, ruff clean, pyrefly 0 errors

## Remaining

Nothing on this branch. Stacked follow-ups, in order:

- [ ] `feat/kit-reorder-assets` (stacked on this) — `reorder.js`/`reorder.css` + `lotek_kit/flask_assets.py`
- [ ] lotek core: `lotek-kit` as a BASE dependency (not the `extensions` extra — core's PR-gate lane runs
      `uv sync --extra dev` and installs no extensions)
- [ ] scribble consumes the kit; evidence gallery converts first, then the findings board (#153)

## Notes / gotchas

- **There is deliberately no `require()` / version guard.** The obvious design — raise on version
  mismatch inside an extension's `register()` — is a **security hole**, not a safety net:
  `app.register_blueprint` is irreversible and has already run; `_inject_host`
  (`lotek:src/app/extensions.py:959`, the only writer of `extras["host"]`, `extras["can_view_client"]`,
  `extras["can_write"]`) runs *after* `register()` returns at `:1112`; and `mount_extensions` swallows
  the raise and continues at `:1157-1164`. The result would be a **mounted surface with no injected
  authorization**. Skew is instead made impossible by construction — see the pin recipe in #149.
- **`kit/` must never carry a `lotek-extension.toml` or a `lotek.extensions` entry point.** Discovery
  enumerates only that entry-point group (`lotek:src/app/extensions.py:361,418`), so their absence is
  what makes the kit structurally unmountable. A guard test pins this.
- **`static/` lives INSIDE `lotek_kit/`, never at the project root behind `force-include`.** That is
  what dodges the trap in CLAUDE.md where an editable path install skips the wheel build and the
  force-included file is silently absent at runtime.
- **The schema id rename is NOT in this PR.** `attackpath/v1` can only replace `vector.attackpath/v1`
  once vector is gone (#159) — until then the mounted vector normalizer re-stamps the old id at
  `vector/vector/schema.py:314`, so renaming early makes a core PR-gate test assert a value the
  production path discards. `LEGACY_SCHEMA_IDS` is accepted on read from day one; nothing emits the
  new id in anger yet.
- Correction for anyone reading #148: its standing hazards say "lotek is CSP-strict, no CDN scripts".
  That is true of **scribble**, not of core — `lotek:src/app/__init__.py:517-521` allowlists
  `cdn.jsdelivr.net` and `cdn.socket.io`. The kit still ships no CDN reference.

- **Known gap left alone deliberately.** `_SUBPROJECTS` in the gate was `("cream", "registrar",
  "scribble", "vector")` — `bugreport` and `exploiteer` are subprojects on disk and were already absent,
  so staged Python under them skips the per-project pyrefly pass. This branch adds `kit` and leaves that
  pre-existing gap alone rather than changing gate behaviour for code it does not touch. Worth its own
  issue.
- **`hatchling` is in the `dev` extra**, not just `[build-system]`: `hatch_build.py` subclasses
  `MetadataHookInterface`, so the parity test cannot import the module without it. Runtime dependencies
  stay empty, and a test asserts that.
- Running the suite on a shared box needs a private `TMPDIR` (`TMPDIR=... uv run --extra dev pytest -q`)
  — `/tmp/pytest-of-<user>` collides between parallel jobs and `tmp_path` then fails with an ownership
  error that looks like a test bug and is not.
