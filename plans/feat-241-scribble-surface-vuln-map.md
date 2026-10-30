# Plan: feat/241-scribble-surface-vuln-map

- **Branch:** `feat/241-scribble-surface-vuln-map`  (worktree: `.claude/worktrees/issue-241`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose
ext#241 — the vuln-map (finding→vuln automation brain `promote_job` resolves through) is under-surfaced:
reachable only as a corner button on the vuln library. Give it first-class discoverability and make
its role in automated promotion legible to operators.

## Done
- [x] Read the feature: `library_ui.py` vuln-map routes, `vuln_map.html`, `promote.py`
      `resolve_vuln_template` precedence, `base.html` nav, mounted-vs-standalone base.

## Remaining
- [ ] Sidebar nav link to vuln-map (`base.html`) — standalone primary nav.
- [ ] Library-landing discovery panel with label + one-line description + link (`library.html`) —
      works mounted AND standalone (the acceptance's "library landing").
- [ ] Strengthen the vuln-map page explainer: name it the finding→vuln automation brain, state the
      full resolution precedence (`vuln_map.html`).
- [ ] Surface the `tag` and `cwe_id` rule columns on the map — they ALSO drive auto-promotion but
      today render as all-dash invisible rows (`library_ui.py` mapping dict + `vuln_map.html` table).
      This is the "make the mapping rules visible" want.
- [ ] Test: assert the nav link, the landing panel + description, and tag/cwe columns render.
- [ ] ruff + pyrefly clean; `cd scribble && uv run --extra dev pytest -q` green.

## Notes / gotchas
- Mounted mode: host provides nav; scribble's own sidebar isn't shown. The library-landing panel is
  the portable surface — put the primary discoverability there, sidebar is the standalone bonus.
- Resolution precedence (promote.py): dedupe_prefix (longest) > source+title glob > tag > cwe > source.
- Keep endpoint name `scribble.vuln_map`. No-JS form POSTs; don't add JS.
