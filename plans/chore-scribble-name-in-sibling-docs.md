# Plan: chore/scribble-name-in-sibling-docs

- **Branch:** `chore/scribble-name-in-sibling-docs`  (worktree: `.claude/worktrees/renamerefs`, off `origin/main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose

The last 16 `Fraction`/`fraction` references in this repo that are **wrong** rather than deliberate. The
rename (#4) missed them for two distinct reasons, both worth recording:

1. **`scribble/scribble/static/lib/VENDOR.md` (10)** — the rename script excludes `lib/`
   (`--exclude-dir=lib`) to protect vendored third-party JS. Correct exclusion; it took a *documentation*
   file down with it. Every path this file cites (`fraction/collab/crdt.py`, `fraction/static/collab.js`,
   `fraction/collab/pm_yjs.py`, `fraction/static/editor.js`) was **dead** — inside the shipping extension.
2. **`vector/**` (4) and `registrar/**` (2)** — sibling extensions citing "the Fraction pattern" as the
   thing to copy. Out of the rename's scope, which was the renamed subdir.

## Done

- [x] `scribble/scribble/static/lib/VENDOR.md` — 10 refs; all four cited paths verified to exist under
      their new names before writing them in.
- [x] `vector/README.md`, `vector/lotek-extension.toml`, `vector/vector/db.py`,
      `vector/vector/standalone.py` — 4 refs, all comments/prose.
- [x] `registrar/README.md`, `registrar/registrar/drivers.py` — 2 refs, both comments/prose.
- [x] Three-token case-sensitive sed; every hunk read by eye; `grep -rnE "\bfaction|FACTION"` over the
      target files was **empty beforehand**, so no FACTION-tool reference was at risk.
- [x] `uvx ruff check vector registrar scribble` clean; `vector` **31 passed / 1 skipped**.

## Remaining

- [ ] Re-vendor `scribble` + `vector` into lotek (separate PR there) so `extensions/` carries this.

## Notes / gotchas

- **No code changed** — every edit is a docstring, a comment, or Markdown. `vector/lotek-extension.toml`
  is the only manifest touched and only its comment line, not a key.
- **`registrar` has no `tests/`** and is not vendored into lotek at all yet, so its two refs ship nowhere
  today; fixed for consistency and because the next person to read `drivers.py` should not be sent to a
  name that no longer exists.
- The residual `fraction` hits elsewhere in this repo are **intentional** and must not be swept:
  `scribble/scribble/db.py` (the `_rename_from_fraction` migration), `scribble/tests/
  test_rename_from_fraction.py`, `plans/chore-rename-fraction-to-scribble.md` (history),
  `scripts/gh-app-token.py` (names PR #1's branch), and two uses of the English word in `cream/tests/`.
- Independent of `lotek-extensions#6` (still open at the time of writing) — that one only touches
  `scribble/tests/`, which is not vendored.
