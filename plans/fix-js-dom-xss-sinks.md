# Plan: fix/js-dom-xss-sinks

- **Branch:** `fix/js-dom-xss-sinks`  (worktree: `~/wt/lotek-extensions/dom-xss`, off `main` f9f9b1b)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose
Close the open CodeQL `js/xss-through-dom` alerts on main (#29, #28, #27 reporting-editor.js; #13, #10
vector-editor.js; #6 scribble artifacts.js). For each alert: trace it, say whether it is exploitable, and
prove it with a browser test. Then remove the HTML-string and unchecked-URL sinks either way, following
PR #288's vector-viewer approach.

## Done
- [x] kit reporting-editor.js: link/image URLs pass a scheme allowlist, go in via setAttribute + encodeURI,
      and the serializer applies the same rule. No innerHTML left. Tests: `test_reporting_editor_dom.py`
      (Chromium), static sink guard in `test_reporting_editor_asset.py`.
- [x] vector-editor.js: panels built from nodes; esc() removed; the save redirect is relative and the id
      encoded. Tests: `test_e2e_editor_dom.py`; `test_viewer_static.py` covers the editor.
- [x] scribble artifacts.js: gallery row built from nodes; URLs behind safeUrl. Tests:
      `test_e2e_artifacts_dom.py`, `test_artifacts_static.py`.
- [x] Rendered DOM compared against main for representative inputs: identical in all three files.

- [x] Polly/human decision (2026-09-28): no sanitizer, no HTML sink, links only (http/https/mailto/
      same-origin relative; no data:, //host, whitespace, control chars or backslash; off-site links
      target=_blank rel="noopener noreferrer").
- [x] Gates green: ruff, pyrefly, kit 162 / vector 206 / scribble 1805 collected.

## Remaining
- [ ] PR review + merge; CodeQL result on the PR.

## Notes / gotchas
- reporting-editor.js has no mirrored copy (scribble and bugreport load the kit's), so no parity test.
- Exploitable: #28 (stored javascript: link href). It runs on click when `_internal.docToFragment`
  output is shown outside a contenteditable. Inside the mounted editor, Chromium does not follow it.
  #10 is exploitable only with an un-normalized `#ved-model` island (edge `at` / phase `n` were printed
  unescaped). `edit_diagram` normalizes it today. #29, #27, #13 and #6 are not exploitable (img src
  never runs script, blob: previews, server-controlled URLs/ids).
- Stored link marks keep their author-written text (not the URL). The directive's "link text is the URL"
  applies to linkified plain text, and none of these files linkifies plain text.
- Playwright is now in the kit's dev extra. The lock is pinned to 1.61.0 so it uses the same Chromium
  build as vector and scribble.
