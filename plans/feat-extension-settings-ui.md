# Plan: feat/extension-settings-ui

- **Branch:** `feat/extension-settings-ui`  (worktree: `.claude/worktrees/ext-settings-ui`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress
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
- **Verdict:** _filled after the candidate run._

## Done

- [x] Design agreed with the lotek-side plan.

## Remaining

- [ ] `[[settings]]` in `vector/lotek-extension.toml` + `deps.host_setting()` + `render.py` footer
- [ ] `vector_user_prefs` + `/vector/settings` + the ⚙ cog in vector's appbar
- [ ] tests
- [ ] `vector/docs/VECTOR.md`

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
