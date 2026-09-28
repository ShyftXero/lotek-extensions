# Plan: feat/vector-tour-mode

- **Branch:** `feat/vector-tour-mode`  (worktree: `~/wt/lotek-extensions/vector-tour`, off `main` 6322f79)
- **PR:** not opened yet
- **Status:** 🟢 ready for review

## Purpose
Vector ships a built-in, read-only operator walkthrough of lotek itself ("How lotek works"), rendered by
the existing viewer in a new neutral **tour mode** (`meta.mode = "tour"`).

## Done
- [x] `meta.mode = "tour"` normalized (vector schema + kit mirror); viewer hides Red/Blue tabs, neutral wording
- [x] per-step target highlight on the map (tour mode only)
- [x] per-step `image` + `links[]`, sanitised in normalize() and again in the viewer
- [x] `#step-N` deep links (replaceState)
- [x] CSP: no `style="..."` via innerHTML, JSON-block boot, Playwright CSP test
- [x] bundled `examples/lotek-walkthrough.json` + `.claims.toml` sidecar
- [x] seed_defaults upserts builtins
- [x] `_norm_node` preserves `activateAt`

## Remaining
- [ ] PR

## Notes / gotchas
- `kit/tests/test_port_parity.py` diffs the kit against vector's schema on **origin/main**, so a change
  mirrored into both copies in one PR reads as drift until it merges. The new lines are listed as
  SANCTIONED_DIVERGENCES with that reason.
- Follow-ups (not here): kit public viewer copy under `/_kit/`; core `/docs` + landing mount of the tour
  (core must add DATAFLOW.md to the docs manifest under id `dataflow` for the step links to resolve);
  bump core's extension pin so the core doc-claims guard validates the sidecar anchors.
- Pre-existing, not fixed here: the viewer's `buildRail()` is never called, so the progress rail never
  renders in either mode.
- Review (subagent) found no blockers. Its Unicode-whitespace concern was already handled (`\s` is
  Unicode-aware in both Python and JS); those cases are now pinned in `test_links_drop_offsite_targets`.
