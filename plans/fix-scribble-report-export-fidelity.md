# Plan: fix/scribble-report-export-fidelity

- **Branch:** `fix/scribble-report-export-fidelity`  (worktree: `.claude/worktrees/report-export-fidelity`, off `origin/main`)
- **PR:** not opened yet — stacked onto the orchestrator's integration branch (no PR from this session, by direction)
- **Status:** 🟢 ready to merge — stacked for the orchestrator's integration branch (no PR from this session)

## Purpose

Two issues on the SAME surface — scribble's report render/export path — so one branch:

- **ext#115 (BUG-7)** — the attack path is in the HTML export (an animated `<iframe
  class="attack-path-frame">`) and **entirely absent from the DOCX**: the string `Attack path` does not
  appear in `word/document.xml`. The Word deliverable silently drops content the HTML deliverable has.
  Print-to-PDF has the same hole (an animated iframe printed at whatever phase it happens to be on).
- **ext#117 (FEAT-4 residual)** — evidence captions are bare `<figcaption>` prose with no `Figure N — `
  numbering, so body text has nothing to cross-reference. Numbering must be **identical** in HTML and
  DOCX, with stable anchor ids.

Also in scope (same print/export surface, cheap in the same pass): **re-verify BUG-3** (ext#77 —
severity colour on paper) and **BUG-4** (ext#78 — Methodology without a checklist). The 2026-08-26
retest did not cover them and the reporter's document still lists them as observed.

## Assessments / Evals

**Hypothesis:** the DOCX drops the attack path because there is no renderer for it at all (the
`.docx` template has no diagram loop and `render_report_docx` appends only checklists + the evidence
appendix), and figure numbering does not exist in either renderer.

**Graders** (deterministic; no LLM judge — every claim here is a string/count over a rendered file):

| Grader | What it measures | Scope |
| --- | --- | --- |
| `/home/shyft/tmp/gf/baseline.py` (ad-hoc EDD harness, not committed) | Outcome set over ONE engagement carrying every figure-bearing surface: per-finding evidence, a nested child's evidence, an engagement-level artifact, and a linked vector diagram. Renders both deliverables and counts what is in `word/document.xml` / `word/media/` and in the HTML. | before + after |
| `cd scribble && uv run --extra dev pytest -q` | the floor; unconditional | before + after |
| `cd vector && uv run --extra dev pytest -q` | the floor for the vector-side print change | before + after |
| `tests/test_report_print_media.py` (Chromium + poppler, pixel-level) | BUG-3 re-verification — it rasterizes the printed page and counts severity-coloured pixels | verify only |
| `tests/test_report_nav_and_methodology.py` | BUG-4 re-verification | verify only |

**Simulate inputs, never outputs:** the harness synthesizes the engagement, the artifacts and a real
`vector.attackpath/v1` model wrapped in vector's own `export.html` shape. It never hand-writes the
rendered DOCX/HTML — it renders it and reads it back with `zipfile` + `python-docx`.

### Baseline (measured on `origin/main` @ a35eff7, 2026-08-26)

```json
{
  "docx.bytes": 38243,
  "docx.drawing_elements": 4,          // all 4 are evidence screenshots
  "docx.media_files": 1,               // (one distinct PNG, deduped by python-docx)
  "docx.has_attack_path_str": false,   // <- ext#115, reproduced exactly
  "docx.has_diagram_caption": false,
  "docx.zone_titles_present": 0,
  "docx.node_labels_present": 0,
  "docx.phase_titles_present": 0,
  "docx.figure_labels": [],            // <- ext#117
  "html.has_attack_path_iframe": true,
  "html.figcaptions": 5,
  "html.figure_labels": [],            // <- ext#117
  "html.fig_anchor_ids": [],
  "html.print_color_adjust": 3,
  "html.has_methodology_section": true
}
```

### Target (after)

- `docx.has_attack_path_str: true`, `docx.has_diagram_caption: true`, zone/node/phase labels present.
- `docx.figure_labels == html.figure_labels`, both `["Figure 1" … "Figure 5"]`, and
  `html.fig_anchor_ids == ["fig-1" … "fig-5"]`.
- Nothing else in the outcome set moves (no evidence drawing lost, HTML iframe still there).

### Measured after

Every target hit, and nothing else in the outcome set moved:

```json
{
  "docx.has_attack_path_str": true,     // was false
  "docx.has_diagram_caption": true,     // was false
  "docx.zone_titles_present": 3,        // was 0
  "docx.node_labels_present": 3,        // was 0
  "docx.phase_titles_present": 2,       // was 0
  "docx.figure_labels": ["Figure 1" … "Figure 5"],   // was []
  "html.figure_labels": ["Figure 1" … "Figure 5"],   // was []  — IDENTICAL to the docx
  "html.fig_anchor_ids": ["fig-1" … "fig-5"],        // was []
  "docx.drawing_elements": 4,           // unchanged — no evidence image lost
  "docx.media_files": 1,                // unchanged
  "html.has_attack_path_iframe": true,  // unchanged
  "html.figcaptions": 5,                // unchanged
  "html.print_color_adjust": 3,         // unchanged (BUG-3)
  "html.has_methodology_section": true  // unchanged (BUG-4)
}
```

## Done

- [x] Claimed #115 + #117 (`status:todo` → `status:doing`, `### CLAIM` comment naming this branch).
- [x] Worktree + split-identity git config.
- [x] Baseline recorded, then re-measured on the same scope (above).
- [x] DOCX carries the attack path (ext#115).
- [x] Print path shows the final keyframe, not an arbitrary animation frame (ext#115).
- [x] Continuous `Figure N — ` numbering + `#fig-N` anchors, identical in HTML and DOCX (ext#117).
- [x] Red-then-green transcript for every guard added (12 mutations, all red then green).
- [x] BUG-3 / BUG-4 re-verified — **neither regressed**; see below.
- [x] Independent security review; four findings fixed on this branch (see below).
- [x] Docs updated on this branch (`scribble/docs/SCRIBBLE.md`, `vector/docs/VECTOR.md`).

## BUG-3 / BUG-4 verdict

- **BUG-3 (ext#77 — severity colour on paper): genuinely fixed, not regressed.**
  `tests/test_report_print_media.py` ran for real here (Chromium + poppler present, **0 skips**): it
  renders the deliverable to a `file://` URL, emulates print, prints to PDF and *counts
  severity-coloured pixels* per page — an operator-level check passes either way, so it rasterizes.
  `print-color-adjust: exact` is present and effective on the severity bar/legend, the severity
  tags/badges and the metric/methodology tiles, and the light paper palette is pinned above the
  dark-theme selectors. 31 passed across that file plus the methodology file.
- **BUG-4 (ext#78 — Methodology without a checklist): genuinely fixed in the HTML, not regressed.**
  `test_methodology_renders_with_no_checklist_at_all` and
  `test_the_methodology_anchor_is_a_section_not_an_empty_div` both pass.
- **New, adjacent, NOT fixed here:** the `.docx` has **no Methodology section at all**, with or
  without a checklist (`_append_checklists` returns early on an empty `ctx.checklists`, and even with
  one it emits only the checklist items, never the standing prose the HTML always carries). Same "the
  `.docx` silently carries less" class as BUG-7, different section, different fix (the prose lives as
  HTML constants in `render_html.py`). **Filed as ext#118** rather than widening this branch — and it
  may well be what the reporter was still seeing when they listed BUG-4 as observed.

## Security review findings — all fixed on this branch

An independent review of the branch diff found four, all in the new untrusted-snapshot path:

1. **HIGH — ReDoS.** `_VAP_MODEL_RE` had two unanchored `[^>]*` runs before a literal, so every
   `<script` in `embed_html` was a start position costing O(n). Measured: `"<script " * 8000` (64 KiB)
   = **15.9s**, extrapolating to ~71 min at 1 MiB and ~76 h at 8 MiB — all inside the link route's
   10 MiB cap, on an engine that holds the GIL and never yields to the gevent hub, i.e. the whole
   worker wedges on any `GET report.docx` by any viewer. Lowering the size cap does not fix a
   quadratic. Replaced with three linear `str.find` scans (`_find_model_blob`). Re-measured: **8 MiB
   in 0.021s**.
2. **MEDIUM — `OverflowError`.** `int(float("inf"))` raises `OverflowError`, an `ArithmeticError`, not
   a `ValueError`; JSON's non-standard `Infinity` (and an overflowing `1e999`) produce that float. One
   token made every future `.docx` export of that engagement an uncaught 500. Fixed at the parser
   (`json.loads(..., parse_constant=...)`) *and* in the `except`.
3. **MEDIUM — XML-illegal control characters.** 23 codepoints lxml refuses survive `str.split()`, and
   the link route's NUL scrub cannot see them: the JSON carries them as the six ASCII characters
   `\u0000`, so the stored snapshot holds no literal control byte. Fixed with one `_xml_safe` scrub at
   the two places untrusted text becomes document (`_d_str` and `_numbered_caption`/the diagram title).
4. **MEDIUM — uncapped `nodes` and uncapped diagram count.** `nodes` was the one list without a cap
   and every node sharing a `(zone, row)` concatenates into one cell (one `<w:br/>` each): 100k nodes
   measured 8.8s / 204 MB peak per render, multiplied by however many diagrams are linked (nothing
   capped that either). Added `_MAX_DIAGRAM_NODES` and `_MAX_DIAGRAMS`, with the shortfall NAMED in
   the document rather than truncated silently.

## Remaining

- [ ] Nothing on this branch. It is stacked for the orchestrator's integration branch: **no
      `--ack-tests`, no PR**, issues stay at `status:doing`.

## Notes / gotchas

- **Why the DOCX gets a native Word rendition and not a screenshot.** A picture in a `.docx` must be
  raster (`python-docx`/`docxtpl` reject SVG; Word's `svgBlip` needs a PNG fallback anyway). vector's
  deliverable is *JS-rendered* — the SVG only exists once a browser has run `vector-viewer.js` — so
  rasterizing it server-side means shipping either a headless browser or a rasterizer into a mounted
  production extension, and re-drawing the diagram in Python means a **second renderer** that drifts
  from the JS the moment anyone touches the viewer. Neither is worth it. Instead the DOCX draws the
  same *geometry the viewer draws* (`zone` → column, `row` → row — see `vector-viewer.js`'s
  `geometry()`) with Word's **native table**, plus the phase walkthrough. It is static, selectable,
  searchable, accessible, font-independent, and — the point of the issue — the Word reader can no
  longer be unaware that an attack path exists.
- **Where the model comes from.** vector's `export.html` embeds the normalized document verbatim in
  `<script type="application/json" id="vap-model">`, with `<`/`>`/`&` escaped as `\uXXXX` (see
  `vector/render.py::json_for_script`), so there is no `</script>` inside it and a non-greedy regex +
  `json.loads` is a complete, deterministic extraction. scribble does **not** import vector (extensions
  are independent — CLAUDE.md); it reads a JSON blob out of a snapshot it already stores, and treats it
  as untrusted (every field is coerced/capped on the way out).
- **Figure numbers are assigned in `context.py`, not in either renderer.** That is the only way "the
  numbering is identical in HTML and DOCX" can be structurally true rather than a coincidence two
  renderers have to keep re-establishing. Canonical order = findings' evidence in board order (a
  parent's own, then each child's) → diagrams → engagement-level evidence appendix, which is the order
  BOTH the `default`/`compliance` HTML templates and the DOCX render in.
- Numbering is assigned to **every** gallery artifact, embeddable or not. Embed success depends on the
  renderer's budget and on whether an artifact reader was supplied, so numbering off it would give the
  same report different figure numbers in HTML and DOCX — the exact failure #117 calls out.
- `feat/scribble-report-themes` is locked and in flight — stay out of theme tokens / CSS plumbing.
