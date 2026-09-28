# Plan: feat/kit-public-vector-viewer

- **Branch:** `feat/kit-public-vector-viewer`  (worktree: `~/wt/lotek-extensions/kit-viewer`, off `main` 1cec7c1)
- **PR:** not opened yet
- **Status:** 🟢 ready to merge

## Purpose
Make vector's viewer loadable on PUBLIC pages (core `/docs`, later the landing page) without a login.
Everything under `/vector/*` is authenticated; core already serves the kit's static files publicly at
`/_kit/`, so the kit ships an exact copy of the viewer.

## Done
- [x] `kit/lotek_kit/static/vector-viewer.{js,css}` — byte-identical copies of vector's; confirmed in the wheel.
- [x] `kit/tests/test_viewer_parity.py` — byte-for-byte parity against vector's copy in this checkout.
- [x] `/_kit/vector-viewer.{js,css}` added to the flask_assets reachability tripwire.
- [x] `test_port_parity.py` — dropped the temporary `tour`/`TOUR`/`activateAt`/`import re` sanctioned
      divergences; #287 is on main and origin/main's schema.py now matches the kit port on those lines.
- [x] Viewer bug: `buildRail()` was never called, so the progress rail was empty in both modes. Now
      called from `setModel`. Playwright test under the strict-CSP harness, both modes.
- [x] `kit/README.md` — "Embedding a tour on a public page".

## Remaining
- [ ] Core: reference `/_kit/vector-viewer.css` + `/_kit/vector-viewer.js` from `/docs` (separate repo).

## Notes / gotchas
- Workflow for any viewer change until vector is deleted (#159): fix `vector/vector/static/`, then `cp`
  into `kit/lotek_kit/static/`. The parity test fails otherwise.
