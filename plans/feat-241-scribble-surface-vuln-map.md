# Plan: feat/241-scribble-surface-vuln-map

- **Branch:** `feat/241-scribble-surface-vuln-map`  (worktree: `.claude/worktrees/issue-241`, off `main`)
- **PR:** not opened yet
- **Status:** 🟢 ready to merge

## Purpose
ext#241 — the vuln-map (finding→vuln automation brain `promote_job` resolves through) is under-surfaced:
reachable only as a corner button on the vuln library. Give it first-class discoverability and make
its role in automated promotion legible to operators.

## Done
- [x] Read the feature: `library_ui.py` vuln-map routes, `vuln_map.html`, `promote.py`
      `resolve_vuln_template` precedence, `base.html` nav, mounted-vs-standalone base.

## Remaining
- [x] Sidebar nav link to vuln-map (`base.html`) — standalone primary nav.
- [x] Library-landing discovery panel with label + one-line description + link (`library.html`) —
      works mounted AND standalone (the acceptance's "library landing").
- [x] Strengthen the vuln-map page explainer: name it the finding→vuln automation brain, state the
      full resolution precedence (`vuln_map.html`).
- [x] Surface the `tag` and `cwe_id` rule columns on the map (`library_ui.py` mapping dict +
      `vuln_map.html` table) — the "make the mapping rules visible" want; they were invisible all-dash rows.
- [x] Tests: nav/landing panel + description + page role + tag/cwe columns (`test_unreachable_ui.py`).
- [x] ruff + pyrefly clean; targeted pytest green (`test_unreachable_ui`, `test_machine_vulnmap`,
      `test_vuln_map_tag_cwe`, `test_vuln_map_seed`).

## Notes / gotchas
- Mounted mode: host provides nav; scribble's own sidebar isn't shown. The library-landing panel is
  the portable surface — put the primary discoverability there, sidebar is the standalone bonus.
- Resolution precedence (promote.py): dedupe_prefix (longest) > source+title glob > tag > cwe > source.
- Keep endpoint name `scribble.vuln_map`. No-JS form POSTs; don't add JS.
- Add form still only creates source/title/dedupe rules; tag/CWE rules are seed-provided and now
  VISIBLE (read) on the map. Adding create-support for tag/CWE was out of scope (visibility, not CRUD).
- Box rule: full suite not run on the fragile HUD host; ran the touched + vuln-map test files only
  (in-memory Flask client). Full `scribble` + lotek mounted suite can run on a non-fragile node if wanted.
