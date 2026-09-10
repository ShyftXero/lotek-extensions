# Plan: feat/scribble-draft-ai

- **Branch:** `feat/scribble-draft-ai` (worktree: `.claude/worktrees/scribble-draft-ai`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose
Phase 3 of the AI engine (core shipped in lotek #729/#730). Give the scribble finding editor a
per-block **"Draft with AI"** action: a button on each prose block (description / remediation / details)
that streams an AI-drafted continuation into the editor via the host hook `ai_stream`, the operator
edits it, and it saves through the existing block autosave. Advisory only — the operator always reviews
before save; it never auto-publishes.

## Dependency / runtime contract
- Uses `host_hook("ai_stream")` (core `app.ai.stream`, injected into `extras`). Shipped on core `main`
  (77e82a4) but **not yet released / re-pinned** — at runtime the hook returns `None` until the deployed
  core provides it, so the feature **fails closed** (button reports "AI unavailable"). Tests inject a
  fake `ai_stream` into `extras`. No core re-pin is required to BUILD or merge this; it lights up once
  core is released and the deployment carries `ai_stream`.

## Changes (paths confirmed by the scribble map)
1. **Streaming route** — a scribble route (`POST …/findings/<id>/blocks/<block>/draft` or similar) that
   resolves `host_hook("ai_stream")`, builds `messages` from the finding (title/severity/target + the
   block's current text + which block), and relays the deltas back as `text/plain` (like core's
   `/settings/advanced/ai-test`). Auth: the actor must be able to **operate on** the finding's engagement
   (`can_operate_on`) — mirror scribble's existing write-gated route pattern. Fail closed (503/inline
   text) when the hook is absent.
2. **Button** — a "Draft with AI" control on each prose block in the finding block editor, gated to show
   only when writable.
3. **JS** — a fetch-reader that streams the route's response into the block's editor field; operator
   edits; existing autosave persists. Token in a `data-` attribute, never interpolated into a `<script>`
   (INV-INPUT-03, learned in #730).
4. **Tests** — the route streams (fake `ai_stream`), fails closed when the hook is absent, enforces
   operate-on auth (non-operator refused); button renders when writable.

## Evals
- **Hypothesis:** an operator can draft prose for a finding block from the model, streamed, and save it
  through the existing autosave; the route is engagement-operate gated and fails closed without the hook.
- **Graders:** `cd scribble && uv run --extra dev pytest -q`; the mounted contract in
  `lotek/tests/test_scribble_extension.py` if a seam there is touched.
- **Verdict:** _after candidate run._

## Done
- [x] Cut branch off origin/main; worktree + split identity set up.
- [x] `draft_api.py` — `POST /rephrase/<block>` relays `host_hook("ai_stream")`, fails closed to inline
      text, inherits the tenancy gate; empty block → first-pass draft, text → rephrase (owner: "rephrase
      this"). Wired into `_wire_feature_routes`.
- [x] `static/draft.js` + `_editor.html` per-block button (gated on `scribble_can_write`) — streams into
      a preview + Copy, via `window.fetch` (host CSRF), `textContent` only.
- [x] `tests/test_draft.py` (5) + a fake `ai_stream` on the conftest stub so the tenancy gate covers the
      route. Full scribble suite green (1575→1576 after the tenancy fix), pyrefly 0 errors.

## Remaining
- [ ] `/security-review` + `/adversarial-reviewer` + `--ack-*`; PR (ext gate is hard — all markers).
- [ ] (later) Lights up in prod only once core ships `ai_stream` to the deployment (on main; a core
      release + this ext re-pinned). Optional follow-up: direct ProseMirror insertion instead of
      copy-from-preview; a mounted test driving the real host `ai_stream`.

## Notes / gotchas
- INV-INPUT-03: no `{{ }}` inside a `<script>` — put any token in a `data-` attribute (bit us in #730).
- scribble prose is ProseMirror `content_json` blocks; the AI draft is text the operator pastes/edits,
  not a direct content_json write — keep the human in the loop.
- Cross-repo: the core commit gate may flag ext files; `RAILS_OVERRIDE=1` is the sanctioned escape if a
  core-side pyrefly false-positive blocks a commit (see memory).
