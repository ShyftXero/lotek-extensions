# Plan: feat/extension-settings-ui

- **Branch:** `feat/extension-settings-ui`  (worktree: `.claude/worktrees/ext-settings-ui`, off `main`)
- **PR:** [ShyftXero/lotek-extensions#136](https://github.com/ShyftXero/lotek-extensions/pull/136)
- **Status:** 🟢 ready for review — merged up to `origin/main`, vector's suite green on this tip

> **The "stacked integration handoff" recorded below never happened.** No orchestrator run occurred and
> `main` moved **49 commits** underneath this branch. Re-measured on 2026-08-28 against current `main`
> — see "Catch-up merge" at the bottom.
- **Issues:** ShyftXero/lotek-extensions#111 · cross-post ShyftXero/lotek#485
- **Paired branch:** `feat/extension-settings-ui` in `ShyftXero/lotek` (the host half)

## Purpose

The extension half of #111: prove the two settings scopes against a real extension (`vector`).
The host half (the `[[settings]]` manifest table, the ⚙ cog on `/settings/extensions`, the admin
gate, the encrypted store and the audit row) lands in lotek core; this branch is the consumer.

## The design decision (recorded in full in the lotek-side plan)

| | **admin / per-install** | **user / per-user** |
|---|---|---|
| where the cog is | the HOST's `/settings/extensions` → `/settings/extensions/vector/config` | the EXTENSION's own appbar → `/vector/settings` |
| who stores it | host, `settings` table, `ext:vector:<key>` | vector, `vector_user_prefs`, keyed on the host user id |
| declared how | `[[settings]]` in `lotek-extension.toml` | not declared to the host at all |
| gate | host's admin gate + host's audit seam | the row's own `owner_id`; no privilege boundary |

This is #111's shaping constraint implemented verbatim: *user settings for an extension live in the
extension itself, reached through its own navigation; admin/global settings live in admin/extensions.*

What vector gets:

- **admin:** `deliverable_footer` — a per-install line stamped into every exported attack-path HTML
  (letterhead class: it is the same for everyone and an operator should not be able to change what
  a client sees). Declared in the manifest, read through the host seam, rendered by `render.py`.
- **user:** `hide_builtin_diagrams` — the seeded example clutters the list forever and whether you
  want to see it is nobody's business but yours. Kept **out** of `visible_diagrams_stmt()` on
  purpose: that function is the IDOR guard, and folding a preference into an authorization predicate
  is how a preference bug becomes a disclosure bug. It filters the already-scoped result instead.

## Evals

- **Hypothesis:** vector can read an admin setting the host stores, and a user can set a preference
  that only affects their own view — with neither able to reach the other's scope.
- **Mode / aggression:** 1 (quick check) — additive, and vector's suite covers both read sites.
- **Capability evals** (must newly pass):
  - [ ] the manifest's `[[settings]]` parses to one admin field — `cd vector && uv run --extra dev pytest tests/test_settings.py -q`
  - [ ] `deliverable_footer` set by the host appears in an exported deliverable; unset → no footer — same file
  - [ ] a user's `hide_builtin_diagrams` hides builtins from THEIR list and nobody else's — same file
  - [ ] the settings page/POST refuses an anonymous (no host actor) caller — same file
  - [ ] `visible_diagrams_stmt()` (the IDOR guard) is unchanged by the preference — same file
- **Regression evals** (must keep passing):
  - [ ] `cd vector && uv run --extra dev pytest -q` — baseline recorded at base sha
  - [ ] `uvx ruff check vector`
- **Graders:** vector's own suite; the mounted contract is graded by the lotek-side branch.
- **Verdict:** all capability evals pass. `cd vector && uv run --extra dev pytest -q` → **74 passed**
  (17 of them new); `uvx ruff check vector tests` clean; `pyrefly check` clean on every changed file.
  **10 guards verified red-then-green.** The mounted half is NOT verified from here — see Notes.

## Done

- [x] Design agreed with the lotek-side plan.
- [x] `[[settings]] deliverable_footer` in `vector/lotek-extension.toml`
- [x] `deps.host_setting()` — the read seam, fail-safe to the caller's default
- [x] `render.py` stamps the footer (autoescaped) — threaded through all THREE export call sites
- [x] `vector_user_prefs` + `GET/POST /vector/settings` + the ⚙ cog in vector's appbar
- [x] 17 tests, 10 guards red-then-green
- [x] `vector/docs/VECTOR.md`
- [x] `[tool.pyrefly] search-path` so the commit gate can type-check this subproject's test files

## Remaining

- [ ] the integration run + PR (orchestrator)
- [ ] a release tag + a lotek-side `[tool.uv.sources]` re-pin, which is what makes the admin half
      reachable in a real browser

## Notes / gotchas

- The admin half **cannot be proven MOUNTED from this repo** — lotek consumes extensions as pinned
  git deps, so `extras["extension_setting"]` only exists once lotek re-pins vector. Vector's suite
  proves it against the injected stub seam (which is what `conftest` exists for); the mounted proof
  is the lotek-side branch's stub-extension test. Called out in the PR rather than implied.
- Vector's browser JSON keeps CSRF on when mounted and has none standalone, so the settings form uses
  the same `{{ csrf_token() if csrf_token is defined else '' }}` guard `base.html` already uses.
- **Deliberately NOT built:** a colour-scheme/theme preference (vector's CSS is a single hardcoded
  dark palette — theming it is its own change, not a settings-plumbing change), per-user settings for
  the other three extensions, and any user preference on the host side.


## Catch-up merge (2026-08-28)

`origin/main` merged in with **no conflicts** — the 49 intervening commits are scribble report
themes/layouts, the exploiteer slice, and the bugreport extension; none of them touch `vector/`.

**The pair was proven MOUNTED, not stubbed.** A wheel was built from this branch's `vector/` and
installed into a lotek worktree carrying the paired host branch. Observed there, in a real Flask app:

1. the host parsed this manifest's `[[settings]]` out of the **wheel-shipped** `lotek-extension.toml`
   (`FieldSpec(key='deliverable_footer', type='str', max=4096.0, secret=False)`) — the force-include
   survived the build;
2. `/settings/extensions` rendered vector's ⚙ cog linking to `/settings/extensions/vector/config`;
3. an admin saved `deliverable_footer`; an operator and a viewer were refused GET **and** POST (403,
   with a valid CSRF token, and nothing written);
4. **the mounted extension read it back** — `POST /vector/api/export.html` returned a deliverable
   carrying the footer, HTML-escaped, via `deps.host_setting` → `extras["extension_setting"]`;
5. vector's own user-scope cog (`/vector/settings`) rendered separately, and `hide_builtin_diagrams`
   appears **only** there — the two scopes did not collapse into one surface.

`vector`: **74 passed** (`uv run --extra dev pytest -q`), ruff + pyrefly clean.

**Still owed, and it belongs to the release, not to this branch:** lotek's `[tool.uv.sources]` pins
`vector` to a tag that predates this declaration, so the host-side mounted test skips until this
branch releases and lotek re-pins.
