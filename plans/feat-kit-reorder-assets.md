# Plan: feat/kit-reorder-assets

- **Branch:** `feat/kit-reorder-assets`  (worktree: `.claude/worktrees/attackpath-kit`, off `feat/kit-skeleton`)
- **PR:** not opened yet — **stacked**, targets `feat/kit-skeleton`, not `main`
- **Status:** 🟡 in progress

## Purpose

The second half of #149: the browser assets the kit exists to stop duplicating, plus the Flask blueprint
that puts a wheel's package data on a URL.

This repo carries **three** hand-written native-HTML5 drag-reorder implementations — core's
`scan-composer.js`, scribble's `artifacts.js` (evidence gallery) and scribble's `board.js` (findings
board). They disagree on the persisted payload, and only one of them can be driven from a keyboard.
`reorder.js` is the one implementation they collapse into.

## Done
- [x] `kit/lotek_kit/static/reorder.js` — pure model layer (`moveItem`, `reorderByKey`, `indexOfKey`)
      plus a DOM layer (`attach`, `arrows`); one frozen global
- [x] `kit/lotek_kit/static/reorder.css` — `lk-`-namespaced affordances
- [x] `kit/lotek_kit/flask_assets.py` — idempotent blueprint, fixed endpoint name, caller-chosen prefix
- [x] Tests: 27 more (71 total on this branch), ruff clean, pyrefly 0 errors

## Remaining
- [ ] Nothing on this branch. Consumers convert in #153, core in a follow-up.

## Notes / gotchas

- **Two layers on purpose.** All three donor implementations buried the ordering logic inside event
  handlers, where nothing could reach it. Here the array functions are pure and the DOM layer calls
  them, so the ordering behaviour is testable without a browser.
- **The module persists nothing.** The three surfaces sit behind different routes with different auth,
  so `attach` reports a move and the caller owns the request. A `fetch` in the shared module would be it
  quietly deciding for all of them — pinned by a test.
- **`reorderByKey` is keyed, not indexed.** A drop reports what it landed *on*, and indices shift the
  moment the dragged element is spliced out. That off-by-one is the bug every hand-rolled version of
  this has had.
- **`registered_prefix()` reads the URL map, not `blueprint.url_prefix`.** That attribute reflects how
  the Blueprint was *constructed* and is `None` when the prefix was supplied at registration time —
  which is every caller here. Reading it would report "not registered" for a working kit.
- **`ensure_registered` must be idempotent.** Flask raises on a duplicate blueprint name and has no
  unregister; with core and one or more extensions all calling at startup, a raise takes the app down
  at boot. First caller wins, and the second is told the effective prefix rather than the one it asked
  for.
- **Call it FIRST in an extension's `register()`.** Not tidiness: if a later statement raises,
  `mount_extensions` catches it and carries on with the extension's blueprint already irreversibly
  registered and its authorization extras never injected. Anything that can fail belongs after the
  things that must not be half-done.
- **`static_url_path=""`** so the served path is `<prefix>/<file>` with no `/static` segment wedged in.
- **The JS guards are static, not behavioural** — there is no JS test runner in this repo. They prove
  the file has not acquired properties that make it unshippable (an external URL under CSP, a stray
  global, a missing export). Real drag/keyboard coverage lands in scribble with #153, which already
  runs Playwright.
- `.gitkeep` was removed from `static/` — the directory now has real contents.
