# feat/vector-viewer-pan-zoom

- **Status:** in progress — viewer change done + tests green (vector + kit); ready for review markers and PR.

## Purpose
Pan/zoom and a couple of readability fixes for the attack-path viewer, driven by Eli's review of the
lotek landing-page walkthroughs (which embed this same viewer). The landing page prototyped these in
its kit copy; this ports them to the canonical viewer so /docs, reports and the landing page all get
them once core re-pins.

## Done
- **Pan/zoom** on the map, entirely via the SVG `viewBox` (no wrapper element, survives every
  re-render because `draw()` calls `applyView`): wheel to zoom (anchored on the cursor), drag to pan,
  and `+ / − / reset` buttons. Clamped to [fit-all, 8×]; the whole map is the drag surface
  (`cursor: grab`). Opt out with `mount(..., { zoom: false })` — then the controls + hint are hidden.
- **Boundary labels moved above/below the zones** (`fw-label` at `BAND_T - 6` / `bandBottom + 16`)
  instead of at mid-height, where a long label like "perimeter firewall" overlapped and clipped the
  node boxes in the narrow column gap.
- **Affordance hint** ("drag to pan · scroll to zoom") + zoom controls, top-right of the map.
- CSP-safe: cursor/transform writes go through the CSSOM (element.style / setAttribute), never inline
  style attributes or an HTML sink, so it holds under `script-src 'self'; style-src 'self'`.
- Applied to BOTH copies of the viewer (`kit/lotek_kit/static` and `vector/vector/static`), kept
  byte-identical (`kit/tests/test_viewer_parity.py`).

## Tests
- `vector/tests/test_e2e_tour.py::test_pan_and_zoom_drive_the_viewbox` — new browser test: buttons +
  wheel zoom, drag pans, reset restores, no CSP violation. Red-then-green transcript captured (disable
  pan/zoom → the hidden control times out the click → fail; restore → pass).
- Existing vector suite + `kit` viewer/asset/parity tests green.

## Remaining
- Review markers (`--ack-review`, `--ack-adversarial`, `--ack-tests`, `--ack-transcripts`), PR, merge
  (auto-tags), then re-pin `lotek-kit` + `vector` in core.

## Notes/gotchas
- The viewer lives in TWO packages (kit + vector), byte-identical — edit one, copy to the other.
- The bounded pan/zoom WINDOW sizing on the landing page is landing-local CSS; this change is only the
  viewer's pan/zoom behaviour + labels, which is what /docs and reports inherit.
