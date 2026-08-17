# Plan: fix/scribble-report-render-sweep

- **Branch:** `fix/scribble-report-render-sweep`  (worktree: `.claude/worktrees/ux-report-sweep`, off `main`)
- **PR:** not opened yet (the orchestrator opens it)
- **Status:** 🟢 ready to merge

## Purpose
Five **client-reported, reproduced** defects in scribble's HTML report renderer, surfaced when an agent
drove the lotek PROD instance over a PAT to author a real TeamsPlus deliverable and the client sent back a
UX punch list. Triage notes + repro scripts + PDF/PNG evidence:
`~/tmp/teamsplus_lotek_notes/lotek_triage/`.

| issue | defect |
|---|---|
| ext#39 | the severity block loses all colour in print/PDF (`@media print` never set `print-color-adjust`; `--sev-*` not pinned for paper) |
| ext#42 | Methodology vanishes with no checklist, leaving a live nav link to an empty anchor |
| ext#45 | back-nav (`← Dashboard` / `← Back to engagement`) sits inside the document masthead, not the toolbar |
| ext#40 | image evidence never renders for a nested-CHILD finding, nor for an ENGAGEMENT-level artifact (`finding_id` null) — both silent, both 201 |
| ext#43 | no cover page, no table of contents, and an executive summary that is one generated sentence plus a dashboard |

They are one branch because they are one file: all five edit `scribble/reporting/render_html.py`, so
parallel worktrees would have fought over it. ext#43 was implemented in a second pass, on top of the
first four.

## Done
- [x] **ext#39** — `print-color-adjust: exact` (+ `-webkit-`) scoped to the elements whose *background*
  carries meaning: `.sevbar`/`.seg`, `.sevlegend .sw`, `.sev-tag`, `.sev-badge`, `.ck-badge`,
  `.chip-toggle`, `.metrics` **and** `.metric` (both — the tile rules are the container's background
  showing through a 1px gap; keeping one and dropping the other prints either no rules or one grey
  block), plus the new `.mth-phases`/`.mth-phase`. **Not** on `*` — `body`/`main`/`.finding` are
  asserted to stay `economy` so the paper ground is never forced.
- [x] **ext#39, second half + more** — the print palette now pins the light `--sev-*` ramp AND is
  selected at 0-2-0 (`:root:not([data-theme="dark"]), :root[data-theme="dark"]`). See "found on the
  way" below: the *entire* print palette override was losing the cascade for a dark-mode viewer.
- [x] **ext#42** — `_render_methodology` always renders: standing phased methodology + per-assessment-type
  framing for the types this report's sections carry (`GroupCtx.type_slug`; prose condensed from
  `skill/scribble-report-refine/references/methodology.md`) + an explicit "no engagement-specific
  coverage checklist was recorded" note. Coverage checklists still own the section when present
  ("Methodology and Coverage") and now own the `#sec-methodology` anchor (it was a bare empty `<div>`).
- [x] **ext#42, the half that matters even if the default is disliked** — the toolbar's section links are
  **derived from the anchors the blocks actually rendered**, in document order. A dangling link is now
  structurally impossible, including for a template that drops a block.
- [x] **ext#45** — back-links moved from `<header class="masthead">` into `.topbar`, ruled off from the
  section jumps. The masthead is now purely the document's title block (eyebrow → title → dates/assessor
  → Confidential), which is also the basis a cover page (ext#43) needs.
- [x] **ext#40(b)** — a nested child's own artifacts render as a compact gallery inside that child's row
  of the *Affected hosts* table (they had no renderer at all; the Evidence cell was the facts line).
  A child row with neither shows `—` instead of a blank cell.
- [x] **ext#40(a)** — `ReportContext` gains an **ADDITIVE** `artifacts` field (engagement-level evidence,
  `finding_id` null) + a new `evidence` block key rendering an Evidence appendix, last in every shipped
  template, absent when there is nothing unattached.
- [x] **ext#40(c)** — the machine upload response echoes `finding_id` + `finding_id_dropped`. Tenancy
  behaviour unchanged (a foreign `finding_id` is still dropped, not 404'd — the comment at the check
  explains why); only its observability changed. The idempotent replay reports the **stored**
  attachment, not what the retry asked for.
- [x] **ext#43(a) — cover page.** A new `cover` block key, first in every shipped template: client
  eyebrow, engagement title, assessment kind, and a fact table (Client · Assessment type · Testing window ·
  Assessor · Report date · Engagement reference) built ONLY from `ReportContext` fields the masthead
  already carried — a row whose field is empty is omitted, not printed as `—`. Plus the `Confidential`
  badge and a standing handling notice. `break-after: page` in `@media print`.
- [x] **ext#43(a), the part that is not cosmetic** — the cover REPLACES the masthead on paper
  (`body.has-cover .masthead { display: none }` in `@media print`). `header.masthead` sits *before*
  `main`, so leaving it visible printed it ahead of the cover and page 1 still would not have been the
  title page. The `has-cover` class is stamped by the renderer only when a cover actually rendered, so a
  template that drops the `cover` block keeps its masthead and still has a title on paper.
- [x] **ext#43(b) — table of contents.** A new `toc` block key: sections at level 1, each section's
  top-level findings (with severity) at level 2. **Derived** from the template's own block list and the
  same conditions the block renderers use, so it lists what the document has and nothing else — reorder or
  drop a block and the contents follow. Group sections gained `id="group-<id>"` (they had only
  `data-group-id`, i.e. nothing to link) and the Compliance Attestation section gained
  `id="sec-compliance"`.
- [x] **ext#43(c) — the executive summary reads as a document, not a dashboard.** The summary now opens on
  front matter: an *Engagement overview* (factual clauses assembled from `scope_type` / dates / `ASSESSOR`,
  each omitted when the field is empty, followed by the existing generated narrative) and a *Scope and
  limitations* statement (point-in-time, absence-of-evidence, non-destructive). The severity bar gained
  rating definitions directly beneath it — the legend showed counts and never said what a rating means,
  which is half of the client's "three plain columns of numbers". The risk banner, bar, tiles and index are
  unchanged and now sit *below* the prose.
- [x] Tests: 4 new modules (13 + 12 + 10 + 27) + 5 cases on `tests/test_machine_artifacts.py`.
- [x] Docs: `scribble/docs/SCRIBBLE.md` — where evidence renders, methodology/nav behaviour, the print
  colour contract, the upload response's new fields, and the cover/contents/front-matter contract.

## Remaining
- [ ] Nothing owed for these five issues. See "explicitly out of scope" below — in particular, the
  **editable** per-engagement prose block (ext#43's third part beyond boilerplate) is deliberately NOT
  half-built here; the exact remaining work is written out below.

## Notes / gotchas

### 🔴 `ReportContext` is a frozen contract and was extended — additively, loudly
`ReportContext.artifacts: list[ArtifactCtx] = field(default_factory=list)` is a **new field with an empty
default**; nothing was renamed, reordered or removed, and every existing consumer (both renderers, the
report routes, the docx path) is unaffected. It exists because the renderers could only ever reach
`finding.artifacts`, so an upload with no `finding_id` was stored, answered `201` with a URL, and could
never appear in any deliverable. `templates.BLOCK_KEYS` also gained `"evidence"` and all three shipped
templates now end with it.

### 🔴 Found on the way: the whole print palette was losing the cascade in dark mode
Bigger than ext#39 as filed, same block. The dark palette is declared on
`:root:not([data-theme="light"])` (0-2-0) and `:root[data-theme="dark"]` (0-2-0); the `@media print`
override used a plain `:root` (0-1-0), so **later source order could not save it**. Measured before the
fix, printing from a dark-mode browser: `body { color: rgb(231,238,245) }` — the dark near-white ink — on
unpainted white paper, for the entire document, not one widget. The print block is now 0-2-0. Guarded by
`test_print_uses_the_paper_palette_from_a_dark_viewer` and
`test_a_dark_template_still_prints_on_paper_colours`.

### 🔴 A PDF-operator check for this defect is VACUOUS — measure pixels
The first version of the PDF test read the fill operators (`r g b rg`) out of the (Flate-compressed)
content streams and asserted the severity colours were present. It **passed against the broken build**:
the same colour is also the finding card's left border and severity-coloured text, and borders/text print
with background graphics off. The test now prints the page twice and compares *painted pixels* of each
ramp colour (poppler `pdftoppm` → P6 PPM, parsed by hand — no image library; skip-clean without poppler):
`print_background=False` must not lose what `True` has. On the fixture: **broken 504 vs 6774; fixed 6773
vs 6774**.

### Toolbar layout had to change with ext#45
Moving the back-links in made three groups + the actions exceed the 1080px measure and Chrome clipped
`← Back to engagement` and `Methodology` mid-word at a 1500px viewport. `.topbar .wrap` now wraps
(`min-height: 52px`, `flex-wrap`), each group is `flex: 0 0 auto`, and `.btn` is `white-space: nowrap`, so
a squeeze moves a whole group to a second line instead of breaking a label. Verified by screenshot.

### 🔴 ext#43: what was NOT built — the editable engagement prose field
The issue asks for three things and the third has two halves. **Standing boilerplate** (front matter that is
true of every engagement) is shipped: it lives in module constants next to `_METHODOLOGY_PHASES`
(`_COVER_HANDLING`, `_LIMITATIONS`, `_SEVERITY_DEFINITIONS`), which is *template-level* prose — one place to
edit, no per-engagement data. **An operator-authored engagement overview is not**, and was not faked:

- There is no field for it anywhere in the model. `Engagement` has `name`/`scope_type`/`company_name`/dates/
  `status`/`guid`/`distribution_list`/`created_by` — and `distribution_list` (`Text`) is itself **dead**:
  declared, never read, never written by any route, so it is not a hook, it is a stub.
- The variable store looks like a shortcut and is not one. `scribble_variables` + `scribble_variable_values`
  do hold engagement-scoped text, and `ReportContext.variables` already reaches them — but there is **no
  route anywhere** (cookie UI or machine API) that creates a custom `TemplateVariable` or sets a
  `VariableValue`. `known_variable_keys` is read once, to populate the editor's token list. So a
  `ctx.variables["ENGAGEMENT_OVERVIEW"]` hook would have been a hook nobody can reach — shipped-and-dead,
  which is worse than absent because it reads as done.
- Doing it properly is: a ProseMirror doc column on `Engagement` (the finding editor's autosave/sanitize
  plumbing is reusable, `prosemirror_sanitize` + `autosave_api`), an editor surface on the engagement page,
  a machine route so a PAT client can author it, a `ReportContext` field, renderer support in **both**
  `render_html` and `render_docx`, and a fallback to the generated narrative. That is a schema + editor +
  API change and belongs in its own branch.

The generated narrative therefore remains the only per-engagement prose in the summary — but it is now the
*second* sentence of a titled overview rather than the whole section.

### 🔴 The standing prose is a JUDGEMENT CALL — read it before merging
Same flag the ext#42 methodology text carries, for the same reason. `_LIMITATIONS`, `_COVER_HANDLING` and
`_SEVERITY_DEFINITIONS` in `render_html.py` assert things about how this practice works ("Testing was
non-destructive", "anything outside the agreed scope was not touched", what each severity means and how
urgently to fix it). They are true of this practice and are deliberately tool-free, host-free and
engagement-fact-free — every engagement-specific clause on the cover and in the overview comes from a
`ReportContext` field and disappears when the field is empty. But they are boilerplate a human should sign
off on, because they now appear in a client deliverable over the assessor's name. The parts of ext#43 that
are *not* a judgement call — the cover page, the contents, the two-way TOC completeness guard — hold
whatever you do to the prose.

### The printed contents carry no page numbers
Not an oversight: page numbers in a CSS-paginated TOC need `target-counter()`, which Chrome's print engine
does not implement — and Chrome is what renders this PDF (`page.pdf()` / the browser's own print dialog).
The entries are real anchors, so they are live links in the PDF; what the printed contents give a reader is
the document's shape and order. Baking numbers in would mean a paged-media renderer (WeasyPrint/Prince),
which is a much bigger dependency decision than this issue.

### Explicitly out of scope (other issues, not silently skipped)
- **Vector diagram embed** — ext#48. Note it interacts with this branch: attaching `export.html` as an
  engagement artifact now at least *reaches* the deliverable (Evidence appendix) instead of vanishing.
- **`render_docx`** — the `.docx` renderer was NOT given the standing methodology prose, child-evidence
  images, the engagement-level appendix, the cover page, the contents or the summary front matter. All five
  issues are about the HTML/PDF deliverable the client read; docx parity is a separate change and a separate
  risk surface (docxtpl template edits, and Word owns pagination and its own TOC field).
- **Checklist-editor styling** (ext#44), **findings CRUD parity** (ext#41), **invoice number** (ext#46),
  **onboarding message** (ext#47) — different tracks.

### Acceptance checks (the triage repro scripts, run for real)
`repro/repro_report.py` — the four-artifact matrix, before → after:

```
BEFORE                                            AFTER
PRESENT  control image on PARENT                  PRESENT  control image on PARENT
ABSENT   image on CHILD                           PRESENT  image on CHILD
ABSENT   ENGAGEMENT-level image                   PRESENT  ENGAGEMENT-level image
PRESENT  text artifact on parent                  PRESENT  text artifact on parent
ABSENT   print-color-adjust in print CSS          PRESENT  print-color-adjust in print CSS
<td class="child-evidence"></td>                  <td class="child-evidence"><div class="evidence">…<img …>
```

Browser probe (`.report-nav` placement + every toolbar link resolved), after:

```
report-nav parent=<div class="wrap">  inside .topbar? true  inside .masthead? false
#sec-summary->LIVE | #sec-findings->LIVE | #sec-methodology->LIVE | #sec-evidence->LIVE
```

## Verification (observed, at the branch tip)

```sh
uvx ruff check scribble                                    # All checks passed!
cd scribble && uv run --extra dev pyrefly check \
    scribble/reporting/render_html.py scribble/reporting/context.py \
    scribble/reporting/templates.py scribble/api_pat.py \
    tests/test_report_*.py tests/test_machine_artifacts.py  # 0 errors
cd scribble && uv run --extra dev pytest -o addopts="" -q -rs
# 649 passed, 2 skipped in 307.53s   (rc=0)
#   SKIPPED tests/test_db_additive_migration.py:82  — needs a real Postgres (SCRIBBLE_TEST_PG_URL)
#   SKIPPED tests/test_db_additive_migration.py:136 — needs a real Postgres (SCRIBBLE_TEST_PG_URL)
```

Both skips are pre-existing and unrelated to this branch (they belong to the SoftHostId retrofit and want
a real Postgres). **Nothing in this branch skipped** — Chromium is present, so all 13 print-media browser
tests ran, and poppler is present, so the rasterized-PDF comparison ran too. Note `pytest`'s
`addopts = "-q"` in `pyproject.toml`: passing `-q` again suppresses the summary line, hence
`-o addopts=""` above.

Not run here, and not this branch's gate: the MOUNTED lotek-side suite (`lotek/tests/test_scribble_*`).
Nothing in this change touches the host seam or an authorization path — the API change is two additive
response fields — but the re-pin into lotek is where that gets exercised.

## Red-then-green
Every guard was watched fail against a deliberately broken build, then pass once restored
(`git checkout HEAD -- <file>`). Commands as run, from `scribble/`:

```sh
uv run --extra dev pytest -o addopts="" -p no:randomly --tb=line -W "ignore::pytest.PytestWarning" <module>
```

**1. ext#39 — remove the `print-color-adjust: exact` rule from `@media print`**
```
RED   8 failed, 5 passed   tests/test_report_print_media.py
      assert 'economy' == 'exact'   ×6
      FAILED …test_meaningful_fills_are_marked_print_exact[.sevbar .seg]  (+ .sevlegend .sw, .sev-tag,
             .sev-badge, .metrics, .metric)
      FAILED …test_pdf_printed_without_background_graphics_keeps_the_severity_fills[critical]
             AssertionError: printing with background graphics off lost the critical fill: 504 painted
             pixels vs 6774 with them on — this is what the client received
      FAILED …[high]  17 painted pixels vs 6283
GREEN 13 passed
```

**2. ext#39 — print palette back to plain `:root`, `--sev-*` pin removed**
```
RED   3 failed, 10 passed
      FAILED …test_print_uses_the_light_severity_ramp_from_a_dark_viewer
             printed the wrong severity ramp: rgb(239, 138, 68)
      FAILED …test_print_uses_the_paper_palette_from_a_dark_viewer
             printed the dark ink rgb(231, 238, 245) (paper ink is rgb(16, 32, 46)) onto white paper
      FAILED …test_a_dark_template_still_prints_on_paper_colours
             assert 'rgb(231, 238, 245)' == 'rgb(16, 32, 46)'
GREEN 13 passed
```

**3. ext#42 — `_render_methodology` returns the pre-fix empty `<div id="sec-methodology"></div>`**
```
RED   3 failed, 9 passed   tests/test_report_nav_and_methodology.py
      FAILED …test_methodology_renders_with_no_checklist_at_all
             assert '<section class="sec group" id="sec-methodology">' in …
      FAILED …test_the_methodology_anchor_is_a_section_not_an_empty_div
      FAILED …test_framing_covers_only_the_assessment_types_this_report_carries
GREEN 12 passed
```

**4. ext#42 — nav back to a fixed list (`nav_keys = tuple(_NAV_LABELS)`)**
```
RED   5 failed, 17 passed   nav_and_methodology + evidence_targets
      FAILED …test_every_toolbar_section_link_targets_an_anchor_in_the_document[default]
             toolbar links #sec-evidence but no element carries that id     (also [compliance], [dark])
      FAILED …test_a_template_that_drops_the_methodology_block_emits_no_methodology_link
      FAILED …test_no_engagement_artifacts_means_no_evidence_section_and_no_nav_link
GREEN 22 passed
```

**5. ext#45 — back-links emitted inside `<header class="masthead">` again**
```
RED   2 failed, 10 passed
      FAILED …test_back_links_render_in_the_toolbar
      FAILED …test_back_links_are_not_in_the_document_masthead
             assert 'report-nav' not in '<header cla…iv></header>'
GREEN 12 passed
```

**6. ext#40(b) — child evidence cell back to the facts line only**
```
RED   4 failed, 6 passed   tests/test_report_evidence_targets.py
      FAILED …test_artifact_on_a_nested_child_finding_renders
             child-attached evidence is missing from the report
      FAILED …test_every_matrix_row_is_present_exactly_where_expected  on-child.png rendered 0 time(s)
      FAILED …test_child_row_with_no_evidence_shows_a_dash_not_a_blank_cell
      FAILED …test_export_zip_carries_child_and_engagement_evidence
GREEN 10 passed
```

**7. ext#40(a) — `build_report_context(...)` passes `artifacts=[]`**
```
RED   4 failed, 6 passed
      FAILED …test_engagement_level_artifact_renders_in_the_evidence_appendix
             no engagement-level evidence section rendered
      FAILED …test_every_matrix_row_is_present_exactly_where_expected
             engagement-level.png rendered 0 time(s)
      FAILED …test_context_lists_only_unattached_artifacts   assert [] == ['engagement-level.png']
      FAILED …test_export_zip_carries_child_and_engagement_evidence
GREEN 10 passed
```

**8. ext#40(c) — the four response lines echoing `finding_id`/`finding_id_dropped` deleted**
```
RED   5 failed, 22 passed   tests/test_machine_artifacts.py
      KeyError: 'finding_id'  ×5 — test_the_201_echoes_the_effective_finding_id,
      test_an_engagement_level_upload_reports_a_null_finding_id,
      test_a_foreign_finding_id_is_dropped_AND_the_caller_is_told,
      test_a_missing_finding_id_is_dropped_the_same_way,
      test_an_idempotent_replay_echoes_the_STORED_attachment
GREEN 27 passed
```

<!-- Lifecycle: create + commit this FIRST thing when cutting the branch; keep it current; KEEP it —
     it merges to main with the branch as a durable record (since 2026-07-28; see CLAUDE.md
     "Per-branch plan"). -->
