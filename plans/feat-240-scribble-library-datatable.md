# Plan: feat/240-scribble-library-datatable

- **Branch:** `feat/240-scribble-library-datatable`  (worktree: `.claude/worktrees/ext-240`, off `main`)
- **PR:** not opened yet
- **Status:** 🟢 ready to merge

## Purpose
`/scribble/library` renders the vuln-templates table hand-rolled (server-side `?q=&category=&severity=&tag=`
filtering, no sort, no paging). Adopt the core DataTable widget (`data-dt`, the one `finding.html` /
`engagement.html` already use) for client-side filter-as-you-type + column sort + paging, and delete the
python-side search/sort code paths it replaces (ext#240).

## Done
- [x] Traced the widget contract (`lotek/src/app/static/data-table.js`): `data-dt`, `data-dt-page-size`,
      `data-dt-empty`, `<th data-sort[/ data-sort-type=num]>`, `<td data-sort-value>`; built-in global
      search box = filter-as-you-type. Widget loaded by the host base when mounted; degrades to a static
      table standalone (same as the existing scribble data-dt tables).

## Remaining
- [x] Template was ALREADY converted to `data-dt` by #252 (commit 96d8aca) which deliberately kept the
      python filtering "for bookmarked deep-links". #240 = the follow-up: drop those paths.
- [x] `library_ui.py` `library()`: reduced to a plain list (dropped q/category/severity/tag). Kept
      `show_inactive`. Fixed the now-stale template comment.
- [x] Tests: replaced the five server-side search/filter tests with one pin that the legacy params are
      no-ops (all rows render); kept the datatable + inactive-toggle tests.
- [x] ruff + pyrefly clean; `test_library.py` green.

## Notes / gotchas (cont.)
- Pre-existing, UNRELATED failures on origin/main (not touched by this branch): `test_smoke::test_seed`
  and `test_seed_content::{test_seeded_template_count,test_default_template_name_set_is_pinned}` — a new
  `Server-Side Template Injection (SSTI)` template landed in `lotek_vulnerabilities.json` (324 vs pinned
  323) without updating the pins. Separate issue; proven pre-existing by stashing this branch's diff.
- Full suite not run to completion: the Chromium e2e tests (`test_e2e_webui.py`) exceed the sandbox
  timeout. Ran the library-relevant non-e2e tests instead.

## Notes / gotchas
- `_parse_severity` stays — still used by create/update (lines ~217/265), only the list route's use goes.
- Severity pill needs `data-sort-value` (numeric rank) or it sorts alphabetically — `_sevrank` map like
  `engagement.html` already does.
- Tags column stays as pills (acceptance: "ready to consume the core tag component when it lands").
