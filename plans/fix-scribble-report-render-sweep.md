# Plan: fix/scribble-report-render-sweep

- **Branch:** `fix/scribble-report-render-sweep`  (worktree: `.claude/worktrees/ux-report-sweep`, off `main`)
- **PR:** not opened yet (the orchestrator opens it)
- **Status:** 🟢 ready to merge — second adversarial review answered (fourth pass, 2026-08-17: 1 BLOCK fixed, 5 CONCERNs, one of them partly pushed back on with the argument recorded below)

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
- [x] Tests: **5** new modules, and `tests/test_machine_artifacts.py` grown from 21 test functions on
  `origin/main` to 47. Measured at the branch tip (node ids, i.e. after parametrisation, and the function
  count where they differ):

  | module | node ids | functions |
  |---|---|---|
  | `tests/test_report_print_media.py` | 19 | 10 |
  | `tests/test_report_nav_and_methodology.py` | 12 | 10 |
  | `tests/test_report_evidence_targets.py` | 18 | 18 |
  | `tests/test_report_cover_and_toc.py` | 28 | 24 |
  | `tests/test_report_standing_prose.py` | 14 | 5 |
  | `tests/test_machine_artifacts.py` (grown, not new) | 67 | 48 |
  | `tests/test_scribble_machine_tenancy.py` (grown, not new) | 17 | 17 |

  (The earlier version of this line said "4 new modules (13 + 12 + 10 + 28)" — a stale count from before
  the third and fourth passes added cases, flagged as a NIT by the second review. Counts above were
  collected with `pytest --collect-only`, not estimated.) `tests/test_report_print_media.py`'s
  rasterized-PDF guard was also adapted to the new pagination (it counted page 1; the cover page moved the
  severity bar to page 3, so it now counts every page — re-proved RED against ext#39's defect, transcript
  22 below).
- [x] Docs: `scribble/docs/SCRIBBLE.md` — where evidence renders, methodology/nav behaviour, the print
  colour contract, the upload response's new fields, and the cover/contents/front-matter contract.

**Third pass — the adversarial review's findings, answered (2026-08-17).** Four defects the second pass
left behind, all found by reading the shipped output rather than the diff:

- [x] **The print palette pinned five token families and missed `--accent*`.** `--accent-ink` is the colour
  of the client name on the cover, of every front-matter / methodology / finding-block label, and the
  *background* of the "Satisfied" attestation badge — which `print-color-adjust: exact` forces to actually
  paint. A dark-mode viewer printed `#7ee0bc` on white paper: 1.6:1, against 8.3:1 for the paper accent, so
  page 1 of the deliverable had no readable client name while the `--sev-high` badge beside it was fine.
  The existing guard could not see it because it measured `body`, which does not use the accent. Fixed by
  adding the family, plus **a drift guard that diffs the two rules' token sets**
  (`test_the_print_palette_pins_EVERY_token_the_dark_theme_overrides`) so the next token cannot be
  forgotten quietly, plus four per-widget assertions and one on the badge fill.
- [x] **The standing prose asserted work nobody recorded.** See the judgement-call note below — the
  methodology phase "Manual validation" is now "Validation" and is written in the present tense, the lead
  says outright that it is *a standing description of method, not a log of what was done on this
  engagement*, and the third limitations bullet is a coverage bound instead of a non-destructiveness claim.
  New module `tests/test_report_standing_prose.py` (11 cases) pins the rule phrase-by-phrase, across all
  three shipped templates, against a bulk-promoted engagement.
- [x] **An unparseable `finding_id` on the machine upload was silently swallowed.** `_as_int` returned
  `None`, the artifact landed as engagement-level evidence and the 201 answered `finding_id_dropped: false`
  — "you did not ask for one" — about a request that plainly did, which is the exact false reassurance the
  echo fields were added to remove. A core UUID is the value to expect here (scribble's finding ids are
  sequential ints; the host's are UUIDv7). Now `400 invalid finding_id` on both the JSON and the multipart
  surface and on the idempotent-replay path; absent/empty still means engagement-level, and a *well-formed*
  id belonging to another engagement is still dropped rather than 404'd (that case would leak whether the
  id exists; an unparseable one cannot leak anything).
- [x] **Docs claimed more than the code does.** `scribble/docs/SCRIBBLE.md` now scopes the evidence table
  to HTML/PDF and states the `.docx` gaps explicitly (child evidence and the engagement appendix are absent
  — `_build_context` never reads `ctx.artifacts` and `_children_html` is a text-only list), and carries a
  🔴 note that an engagement-level upload is `include_in_report=True` with **no list route and no UI**, so
  the rendered Evidence appendix is the only place it becomes visible: read it before sending the report.

🔴 **A previous session was interrupted mid-branch here** (API session limit) with this third pass sitting
uncommitted in the worktree. The work was reviewed, found coherent, verified (red-then-green transcripts
24–26 below) and committed by the session that picked it up; nothing was discarded.

**Fourth pass — the second adversarial review's findings, answered (2026-08-17).** Verdict was REPAIR with
one BLOCK, and its diagnosis was right: the mechanism chosen for ext#40 (surface engagement-level artifacts
in the report) shipped without the two guards it needed. Everything below was re-measured before it was
fixed, not taken on trust.

- [x] 🔴 **BLOCK — the Evidence appendix inlined EVERY artifact as a base64 `data:` URI.** Reproduced with
  the reviewer's probe shape (three 5 MiB `application/vnd.tcpdump.pcap` artifacts attached at engagement
  level, `inline_assets=True`): **branch tip `rendered HTML 20.0 MiB in 4.0s`, `origin/main` 0.0 MiB in 0.0s
  — main never read those bytes at all.** Two separate defects in one line of code: raw working material
  (pcaps, scan dumps, vector `export.html`) was shipped byte-for-byte inside a CLIENT deliverable, and since
  the upload cap is 25 MiB *per* artifact with nothing capping the count, twenty of them build ~660 MB of
  base64 in one string plus an nh3 pass on **every** report read (both report routes pass
  `inline_assets=True`). Fixed in `_AssetResolver`: only **images** are ever embedded, within
  `_MAX_INLINE_ASSET_BYTES` (8 MiB, checked against `ArtifactCtx.byte_size` *and* against the bytes
  actually read, because the column is advisory) and a per-render `_MAX_INLINE_TOTAL_BYTES` (48 MiB).
  Anything else takes the "not embedded" chip, which now carries the caption and the recorded size so the
  report still SAYS what evidence exists — that is ext#40's point — without carrying it. `export_zip` is
  unchanged and is the delivery path for non-image bytes (a zip entry is a real file). Same probe after the
  fix: **`rendered HTML 0.0 MiB in 0.0s`, 0 data: URIs, and the pcap's bytes are never read.**
  Belt-and-braces on the count as the reviewer asked: the appendix lists at most `_MAX_APPENDIX_ITEMS`
  (200) and states how many it withheld — silently truncating a client deliverable's evidence list would be
  the same silent omission ext#40 is.
- [x] **CONCERN — `_finding_id_or_400` refused strings and accepted JSON numbers.** Confirmed by direct
  probe: `2.9 -> (2, None)`, `True -> (1, None)`, `10**30` straight through. `int()` **coerces** rather than
  validates, so `{"finding_id": 2.9}` attached the evidence to finding 2 — an id the caller never named —
  and answered `finding_id_dropped: false`, i.e. the exact false reassurance the echo fields were added to
  remove, over docs that promise gibberish is refused. The parse is now explicit (`bool` rejected first
  because it is an `int` subclass; `int` accepted; a `str` only when it fully matches `\d+`; a float, list,
  dict or `"1e3"` refused) and does not use `_as_int` at all.
- [x] **CONCERN — an unbounded integer reached `db.get()` and 500'd.** `10**30` raised
  `OverflowError: Python int too large to convert to SQLite INTEGER` (a `DataError` on Postgres, which also
  poisons the open transaction) — and `save_bytes` had already run, so the failed request left an **orphan
  file**. Bounded to `0 < fid <= 2**31 - 1` (`_MAX_FINDING_ID` — the column's bound, not a policy) in the
  parse, which runs before the body is decoded, so the refusal is a clean 400 and nothing is written to the
  table *or* to the artifact directory. `2**31 - 1` itself is still accepted and simply dropped as
  nonexistent, so the bound cannot refuse a legal id.
- [x] **Found on the way (same defect class, other input path): every machine route's `<int:>` id
  converter was UNBOUNDED.** Werkzeug's bare integer converter has no max, so
  `GET /scribble/machine/engagements/<30 digits>` ROUTED and then 500'd inside `db.get()` — measured 500 on
  three routes. Pre-existing on `main`, but it is the *same* finding the review filed against the body's
  `finding_id`, and fixing only the body path would have answered the letter and not the substance —
  especially with two of the new routes adding fresh instances of it. All eight id converters in
  `api_pat.py` now use a shared `_ID = "int(min=1, max=2147483647)"`, so an out-of-range id never reaches a
  view: Werkzeug does not match the rule and answers a routing refusal (404, or 405 where a same-path rule
  with another method exists) instead of 500. Two guards, because a rule expressed as a string constant is
  only as good as the next person remembering it: an end-to-end one on the three measured URLs, and
  `test_every_machine_route_id_converter_is_BOUNDED`, which walks the live `url_map` and inspects each
  `NumberConverter.max`. **Scoped to the machine blueprint**: the cookie blueprints have the same unbounded
  converters and are NOT fixed here — a session-authenticated 500 is a different (and much smaller) risk
  surface than an agent-driven machine API, and sweeping every UI route is its own change.
- [x] **CONCERN — publication by default with no review surface.** Partially accepted, partially pushed
  back on; see "the publish default" below. What was built: `include_in_report` is now honoured on the
  machine upload and echoed in the 201/200; a new `GET /engagements/<id>/artifacts` (`?unattached=1`) is
  the review surface that did not exist; a new `POST /engagements/<id>/artifacts/<artifact_id>` flips
  `include_in_report` (and fixes a caption) over a PAT, which the cookie route cannot do.
- [x] **CONCERN — a tautological test presented as the ext#42 guard.** Correct, and correct about *why*:
  `nav_keys` is derived as `f'id="sec-{k}"' in html`, so asserting the id is present asserted the
  implementation's own predicate back at itself, and it was GREEN against the unfixed build. Rewritten as
  `test_every_toolbar_section_link_LANDS_ON_CONTENT`: the region between the anchor and the next section
  must contain real text. RED against the pre-fix empty `<div id="sec-methodology"></div>` (transcript 27),
  and the docstring now says outright that the ext#42 invariant proper lives in two other guards.
  Deliberately *not* the reviewer's suggested "assert an `<h2 class="sec-h">` and a non-empty `.sec-body`":
  `findings` is anchored by a bare `<div id="sec-findings"></div>` on purpose (a scroll target above the
  groups), so that shape would have been a false positive.
- [x] **CONCERN — the third `_LIMITATIONS` bullet re-stated the deleted claim, past the guard.** Correct on
  both halves. "Systems, accounts and techniques outside them **were not examined**" is the same past-tense
  assertion about conduct as "was not touched", and the phrase blacklist matched the latter and missed the
  former. The bullet now reads "…This report makes no claim about systems, accounts or techniques outside
  them", both wordings are in `FORBIDDEN`, and there is a new guard asserting the ALLOWED wording (so a
  revert cannot pass by deleting the bullet) plus the shape rule that every bullet names the *report* as
  what it is limiting. The module now also says in as many words that a phrase blacklist is weak and has
  already been slipped once — the compensating control is reading the prose.

### 🔴 The publish default: what was NOT changed, and why
The review asked for `include_in_report=False` on uploads with no `finding_id`. **Not done**, deliberately.
That flips ext#40's symptom straight back on for the workflow that filed it: an agent driving the machine API
uploads engagement-level evidence, gets a 201 with a URL, and it appears in no deliverable — which is
verbatim the issue. So the default still publishes, and what was added instead is everything needed for it
to be a *decision* rather than a silent one: `include_in_report` on the upload, echoed in the response; a
list route (`?unattached=1`) that is the first surface anywhere on which that set can be reviewed; and a
PAT-reachable toggle to take one back out.

What that does **not** fix, stated plainly rather than papered over: rows created *before* this change were
created under the old meaning ("unattached" == "not in the report"), and the first render after upgrade
publishes them. There is no discriminator in the schema to tell them apart — no "published deliberately"
column, and `created_at` versus a deploy timestamp would be a guess — so the honest answer is procedural and
is now in `SCRIBBLE.md`: **run the list route on an existing engagement, and read the Evidence appendix,
before sending a report.** The BLOCK fix narrows the exposure a long way on its own: no non-image working
material can ship its bytes any more, only its name.

## Remaining
- [ ] Nothing owed for these five issues. Two things the fourth pass deliberately left, both written out
  where they are decided: the **publish default** for engagement-level uploads (see "The publish default"
  above — the review asked for `False`, the argument for keeping `True` plus a review surface is recorded
  there), and the engagement-artifact list + toggle on the engagement **PAGE** (the cookie/UI half; the
  machine half shipped here). See "explicitly out of scope" below — in particular, the
  **editable** per-engagement prose block (ext#43's third part beyond boilerplate) is deliberately NOT
  half-built here; the exact remaining work is written out below.

## Notes / gotchas

### 🔴 `ReportContext` is a frozen contract and was extended — additively, loudly
`ReportContext.artifacts: list[ArtifactCtx] = field(default_factory=list)` is a **new field with an empty
default**, and (fourth pass) `ArtifactCtx.byte_size: int | None = None` is a second one — what the row
recorded for the file, so the renderer can decide whether to carry its bytes without reading them first.
Both are appended with defaults; nothing was renamed, reordered or removed, and every existing consumer
(both renderers, the report routes, the docx path) is unaffected. Two fields, then, not one — the earlier
wording implied a single addition, flagged as a NIT by the second review. It exists because the renderers could only ever reach
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
`_SEVERITY_DEFINITIONS` in `render_html.py` assert things about how this practice works (what each severity
means, how urgently to fix it, how the document should be handled). They are deliberately tool-free,
host-free and engagement-fact-free — every engagement-specific clause on the cover and in the overview comes
from a `ReportContext` field and disappears when the field is empty. But they are boilerplate a human should
sign off on, because they now appear in a client deliverable over the assessor's name. The parts of ext#43
that are *not* a judgement call — the cover page, the contents, the two-way TOC completeness guard — hold
whatever you do to the prose.

**Two of those claims did not survive that read, and are gone (third pass, see below).** The prose used to
say "Every candidate weakness was validated by hand before it was reported" and "Testing was
non-destructive … anything outside the agreed scope was not touched" — past-tense assertions about work
performed on *this* engagement, in a document the renderer builds without knowing whether any of it
happened. Scribble's headline workflow is `promote-job`, so a real deliverable can be forty findings lifted
straight out of a scan, and there is no flag, field or template that removes the sentence (the template
registry is frozen data, not an editor). The rule now is a hard one and is pinned by
`tests/test_report_standing_prose.py`: standing prose may describe **method** (present tense) and may state
**limitations** (what the report does not claim — under-claiming cannot be false); it may not assert that
particular work was done. Rules-of-engagement statements belong in the per-engagement prose field that is
still to be built.

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

`repro_report.py` after the ext#43 pass — items 2 and 3 flip:

```
PRESENT  item 2  cover page                 ['class="cover"']
PRESENT  item 3  table of contents          ['class="toc"']
ABSENT   item 1  exec-summary boilerplate   ['engagement overview']
ABSENT   item 22 vector diagram embed       ['attackpath']     <- ext#48, correctly still absent
```

**Item 1 reads ABSENT and the front matter is nonetheless there** — the needle is a case-sensitive guess by
the triage author. The rendered document carries `Engagement overview`, `Scope and limitations` and `How
these ratings are used`; it does not carry the lower-case phrase. (It briefly *did*, from a stylesheet
comment — the sheet ships inside the document, so the grep matched a comment rather than the prose. The
comment was reworded rather than left to fake a pass, and the same trap is now noted in the sheet.)

**The client-visible artifact, before → after.** BEFORE is the real client-era PDF kept in the triage
evidence (`evidence/report_nobg.pdf`); AFTER is the same shape of engagement rendered at this branch tip,
both printed the way the client's dialog defaults (`print_background=False`, Letter):

```
BEFORE  Pages: 2
        page 1: REPRO CLIENT  Checklist repro external assessment  CONFIDENTIAL  EXECUTIVE SUMMARY
                OVERALL RISK  1 high  High  This assessment of Repro Co identified 1 finding …
                FINDINGS BY SEVERITY  1  Critical 0  High 1  Medium 0 …

AFTER   Pages: 5
        page 1: REPRO CLIENT  Checklist repro  external assessment  CLIENT  ASSESSMENT TYPE  Repro Client
                external  TESTING WINDOW  ASSESSOR  2026-08-03 – 2026-08-12  e.mcrae  REPORT DATE
                ENGAGEMENT REFERENCE  2026-08-17  #1  CO N F I D E N T I A L  This report describes …
        page 2: CONTENTS  Executive Summary  External  A finding  Methodology  HIGH
        page 3: EXECUTIVE SUMMARY  ENGAGEMENT OVERVIEW  This report covers an external assessment of
                Repro Co. Testing was carried out over 2026-08-03 – 2026-08-12. The assessment was
                performed by e.mcrae. This assessment of Repro Co identified 1 finding … SCOPE AND
                LIMITATIONS  This report describes the environment as …
```

## Verification (observed, at the branch tip)

```sh
uvx ruff check scribble                                    # All checks passed!
cd scribble && uv run --extra dev pyrefly check \
    scribble/reporting/render_html.py scribble/reporting/context.py \
    scribble/reporting/templates.py scribble/api_pat.py \
    scribble/api_schemas.py \
    tests/test_report_*.py tests/test_machine_artifacts.py \
    tests/test_scribble_machine_tenancy.py                 # 0 errors
cd scribble && uv run --extra dev pytest -o addopts="" -q -rs
# first pass  (13ae528): 649 passed, 2 skipped in 307.53s   (rc=0)
# ext#43 pass (22cc9da): 677 passed, 2 skipped in 332.10s   (rc=0)
# third pass (9fa3162): 701 passed, 2 skipped in 916.44s (0:15:16)   (rc=0)
# fourth pass (branch tip): 746 passed, 2 skipped in 462.96s (0:07:42)   (rc=0)   <- +45
#   SKIPPED tests/test_db_additive_migration.py:82  — needs a real Postgres (SCRIBBLE_TEST_PG_URL)
#   SKIPPED tests/test_db_additive_migration.py:136 — needs a real Postgres (SCRIBBLE_TEST_PG_URL)
```

The third-pass run is the whole suite at the tip, after the working tree was restored byte-for-byte from
the pre-transcript copy (`git diff` compared before and after: identical). It is slower than the earlier
two because it was run alongside nothing else on a busier box, not because anything got heavier — the
count is what matters: **+24** over the ext#43 pass — 11 standing-prose cases, 6 accent/drift-guard print
cases (13 → 19 in that module), 7 upload `finding_id` cases (27 → 34). Still 2 skipped, still the same two,
so nothing in the third pass skipped either.

Both skips are pre-existing and unrelated to this branch (they belong to the SoftHostId retrofit and want
a real Postgres). **Nothing in this branch skipped** — Chromium is present, so all 13 print-media browser
tests plus the 4 browser cases and the PDF-page check in `test_report_cover_and_toc.py` ran, and poppler is
present (`pdftoppm` + `pdftotext`), so the rasterized-PDF comparison and the page-1/page-2 text check ran
too. Note `pytest`'s `addopts = "-q"` in `pyproject.toml`: passing `-q` again suppresses the summary line,
hence `-o addopts=""` above.

The printed pages were also **looked at**, not only asserted on: pages 1–3 of the probe PDF rasterized at
80 dpi (cover / contents / executive summary). The severity bar's orange fill is painted on page 3 of a
`print_background=False` PDF, which is ext#39 still holding at the new pagination.

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

### ext#43 pass (second pass, on top of 1–8 above)

Same discipline, same command, run from `scribble/`: break the production code, watch it fail, restore with
`git checkout HEAD -- scribble/reporting/render_html.py`, watch it pass. Module unless stated:
`tests/test_report_cover_and_toc.py` (**GREEN 27 passed** for every one of them).

**9. cover page — `_render_cover` returns `""` (the pre-#43 state: no cover at all)**
```
RED   6 failed, 21 passed
      FAILED …test_the_document_carries_a_cover_page_before_everything_else
             assert 'class="cover"' in '<!doctype html>…'
      FAILED …test_the_cover_omits_facts_the_engagement_does_not_record   no cover page in the document
      FAILED …test_the_cover_escapes_engagement_and_client_names          no cover page in the document
      FAILED …test_the_cover_and_contents_are_print_only   assert 'MISSING:.cover' == 'none'
      FAILED …test_the_masthead_gives_way_to_the_cover_on_paper_only   assert 'block' == 'none'
      FAILED …test_the_printed_pdf_opens_on_the_cover_then_the_contents
             assert 'Treat it as confidential' in 'TEAMSPLUS Web Portal …'   <- page 1 is the masthead
```

**10. cover facts — every row always, empty ones as an em-dash**
```
RED   1 failed, 26 passed
      FAILED …test_the_cover_omits_facts_the_engagement_does_not_record
             assert 'Testing window' not in '<section cl…v></section>'
```

**11. contents — `_render_toc` returns `""`**
```
RED   15 failed, 12 passed
      "no table of contents in the document" ×11 (order, per-template link resolution ×3, per-template
      completeness ×3, template order, dropped block, nested children, no-evidence, escaping, no-findings)
      FAILED …test_the_cover_and_contents_are_print_only   assert 'MISSING:.toc' == 'none'
      FAILED …test_the_printed_pdf_opens_on_the_cover_then_the_contents
             assert 'CONTENTS' in 'EXECUTIVE SUMMARY ENGAGEMENT OVERVIEW…'
```

**12. contents DRIFT — the `evidence` branch dropped from `_toc_entries` (a section that forgot to register)**
```
RED   4 failed, 23 passed
      FAILED …test_the_contents_list_every_section_and_finding_in_document_order
             AssertionError: the contents omit 'Evidence'
      FAILED …test_every_anchored_section_in_the_document_appears_in_the_contents[default]
             these sections are in the document but not in the contents: ['sec-evidence']
             (also [compliance], [dark])
```
This is the guard that matters most: it is the *reverse* direction, and it is the exact failure mode ext#42
and ext#43 are both about — a section going quietly missing from navigation.

**13. contents DANGLING — the Compliance Attestation entry emitted unconditionally**
```
RED   1 failed, 26 passed
      FAILED …test_an_engagement_with_no_findings_has_no_dangling_contents_entries
             assert 'id="sec-compliance"' in '<!doctype html>…'
```
Honest note: the three parametrized `…link_targets_an_anchor…` cases stayed GREEN here, because the full
fixture genuinely *has* a compliance checklist, so that anchor exists for it. The no-findings case is what
catches the dangling link, which is why both exist.

**14. `body.has-cover .masthead { display: none }` removed from `@media print`**
```
RED   2 failed, 25 passed
      FAILED …test_the_masthead_gives_way_to_the_cover_on_paper_only   assert 'block' == 'none'
      FAILED …test_the_printed_pdf_opens_on_the_cover_then_the_contents
             assert 'Treat it as confidential' in 'TEAMSPLUS Web Portal …'
```

**15. `has-cover` stamped unconditionally (the other side of the same rule)**
```
RED   2 failed, 25 passed
      FAILED …test_a_template_without_a_cover_block_renders_none_and_keeps_its_masthead
             assert '<body>' in '<!doctype html>…'
      FAILED …test_a_coverless_template_still_prints_its_masthead   assert 'none' != 'none'
```

**16. `.cover, .toc { display: block }` — the print-only blocks leaking onto the screen**
```
RED   1 failed, 26 passed
      FAILED …test_the_cover_and_contents_are_print_only   assert 'block' == 'none'
```

**17. front matter — `_render_summary` back to the pre-#43 narrative-only body**
```
RED   3 failed, 31 passed   cover_and_toc + tests/test_report_html.py
      FAILED …test_the_summary_leads_with_prose_and_not_the_dashboard
             assert 'class="frontmatter"' in '<!doctype html>…'
      FAILED …test_the_overview_omits_clauses_for_data_the_engagement_lacks
      FAILED …test_the_overview_article_follows_the_scope_word
```
`test_report_html.py` was included deliberately: its narrative test passes against BOTH shapes, which is
the point — the generated narrative was kept, not replaced.

**18. overview — every clause always, with placeholders for missing data**
```
RED   1 failed, 26 passed
      FAILED …test_the_overview_omits_clauses_for_data_the_engagement_lacks
             assert 'Testing was…ied out over' not in '<!doctype h…>\n</html>\n'
```

**19. severity definitions — the `rollup.total <= 0` gate removed**
```
RED   1 failed, 26 passed
      FAILED …test_a_clean_engagement_defines_no_ratings
             assert 'class="sev-defs"' not in '<!doctype h…>\n</html>\n'
```

**20. contents — the entry label interpolated unescaped**
```
RED   1 failed, 26 passed
      FAILED …test_the_contents_escape_a_finding_title
             assert '&lt;img src=x' in '<nav class="toc" id="sec-toc" …'
```

**21. group anchor — `id="group-<id>"` removed from `_render_group`**
```
RED   3 failed, 24 passed
      FAILED …test_every_contents_link_targets_an_anchor_in_the_document[default]
             the contents link #group-1 but nothing carries that id     (also [compliance], [dark])
```

**22. RE-VERIFICATION of ext#39 through the adapted PDF guard** — the cover page moved the severity bar off
page 1, so `tests/test_report_print_media.py` now counts painted pixels across EVERY page
(`_pdf_pages_rgb`) instead of page 1. A guard whose fixture assumption changed has to be re-proved, not
assumed: `print-color-adjust: exact` was deleted again and the adapted test still fails hard.
```
RED   8 failed, 5 passed   tests/test_report_print_media.py
      assert 'economy' == 'exact' ×6
      FAILED …test_pdf_printed_without_background_graphics_keeps_the_severity_fills[critical]
             printing with background graphics off lost the critical fill: 2174 painted pixels vs 8445
             with them on — this is what the client received
      FAILED …[high]  436 painted pixels vs 6702
GREEN 13 passed
```
(The counts differ from the first pass's 504-vs-6774 because the measurement is now the whole document and
the document grew; the ratio is 0.26 and 0.07 against a 0.9 threshold, so the signal is if anything
louder.)

**23. group anchor — `_group_anchor` returns `f"group-{group.id}"`, i.e. `group-None` for the synthetic
*Ungrouped* bucket**
```
RED   1 failed, 27 passed
      FAILED …test_the_synthetic_ungrouped_bucket_is_listed_and_linkable
             assert 'href="#group-ungrouped"' in '<nav class="toc" id="sec-toc" …'
GREEN 28 passed
```
Worth stating why this case needs its own test: `_group_anchor` is shared by the section and the contents
*on purpose*, so breaking it breaks both sides consistently and the link-resolution guard stays GREEN. The
anchor of the one group that has no database id is therefore asserted literally.

### Third pass (the adversarial review's findings)

Same discipline. The break here is the whole pre-fix file — `git show HEAD:<path> > <path>`, i.e. the build
as the second pass left it — because that IS the state each new guard was written against. Restored from a
byte-identical copy afterwards and re-verified (`git diff` before and after the exercise are identical).

**24. standing prose — `render_html.py` back to its pre-third-pass state**
```
RED   11 failed   tests/test_report_standing_prose.py   (the whole module; nothing passed)
      FAILED …test_the_report_asserts_no_unrecorded_work[was validated by hand]
      FAILED …[Testing was non-destructive]  [was not touched]  [no destructive action was taken]
      FAILED …[Testing followed]  [was not observed in this environment]
      FAILED …test_no_shipped_template_can_reintroduce_the_claims[default]  (also [compliance], [dark])
             AssertionError: template 'default' asserts 'was validated by hand'
      FAILED …test_the_methodology_says_it_is_a_standing_description_not_a_work_log
      FAILED …test_the_limitations_still_say_what_the_report_does_NOT_claim
             assert 'bounded by the agreed scope' in '<!doctype html>…'
```
All six forbidden phrases were present in the shipped document, and all three templates carried them —
which is the point of the per-template case: there is no template-level escape from this prose.

**25. print palette — same pre-fix file, the `--accent*` family unpinned**
```
RED   7 failed, 12 passed   tests/test_report_print_media.py
      FAILED …test_print_uses_the_paper_palette_from_a_dark_viewer
             the CLIENT NAME on the printed title page came out rgb(126, 224, 188)
      FAILED …test_accent_text_prints_in_the_paper_accent_from_a_dark_viewer[.cover-eyebrow]
             (also [.fm-block h3], [.mth-k], [.finding-body .block-label]) — rgb(126, 224, 188)
      FAILED …test_the_print_palette_pins_EVERY_token_the_dark_theme_overrides
             the @media print palette does not pin ['--accent', '--accent-ink', '--accent-wash']
      FAILED …test_the_compliance_badges_print_readably_from_a_dark_viewer
             SATISFIED printed as white text on rgb(126, 224, 188)
GREEN 42 passed   (standing_prose + print_media + nav_and_methodology, after restoring the file)
```
Note which 12 stayed GREEN: every `print-color-adjust` case and both severity-ramp cases. That is the
omission's fingerprint — one family missing out of six, with the neighbouring badge printing correctly.

**26. `finding_id` parse refusal — `api_pat.py` back to its pre-third-pass state**
```
RED   6 failed, 28 passed   tests/test_machine_artifacts.py
      FAILED …test_a_finding_id_that_does_not_PARSE_is_refused_not_silently_dropped
             [0198f3c1-6a1e-7c0b-9a3e-2f5c8d7b4a11]  [abc]  [12.5]  [bad3 = the empty list]
             AssertionError: {'finding_id': None, 'finding_id_dropped': False, …}   <- the false 201
      FAILED …test_a_multipart_upload_refuses_an_unparseable_finding_id
      FAILED …test_an_unparseable_finding_id_is_refused_on_a_REPLAY_too
GREEN 34 passed
```
`test_an_empty_finding_id_still_means_engagement_level` stayed GREEN on both sides **on purpose**: it pins
the behaviour that must NOT change, so a fix that refused `""` as well would fail it.

### Fourth pass (the second adversarial review's findings)

Same discipline: break the production code, watch it fail, restore, watch it pass. Restores verified with
`git diff --stat` before and after (identical). Commands from `scribble/`, `-o addopts="" -q`.

**27. the nav guard — `_render_block_by_key` back to `origin/main`'s empty
`<div id="sec-methodology"></div>`**
```
RED   3 failed, 9 deselected   tests/test_report_nav_and_methodology.py -k LANDS_ON_CONTENT
      (all three shipped templates: default, compliance, dark)
GREEN 3 passed
```
This is the transcript that matters most in this pass, because it is the one the review said could not
exist: the OLD assertion (`id="sec-methodology"` appears somewhere) was GREEN in exactly this state.

**28. the inlining budget — `_AssetResolver` back to inlining everything unbounded, appendix cap removed**
```
RED   7 failed, 11 passed   tests/test_report_evidence_targets.py
      FAILED …test_a_non_image_artifact_is_NAMED_but_its_bytes_stay_out_of_the_document
      FAILED …test_the_bytes_of_a_non_image_are_never_even_READ
             AssertionError: the renderer read bytes it cannot embed: ['scan.xml', 'shot.png']
      FAILED …test_an_image_over_the_PER_ASSET_budget_is_not_embedded
      FAILED …test_a_LYING_byte_size_does_not_get_an_artifact_past_the_budget
      FAILED …test_the_PER_RENDER_budget_bounds_a_document_full_of_legal_images
             AssertionError: the render embedded 10 images, i.e. 40960 bytes
      FAILED …test_the_appendix_lists_at_most_MAX_items_and_SAYS_how_many_it_withheld   assert 5 == 3
      FAILED …test_the_not_embedded_chip_reports_the_size_it_is_not_carrying
GREEN 18 passed
```
The 11 that stayed GREEN are the ext#40 matrix rows themselves — which is the control that matters here: the
budget must not cost the issue its fix. Every image still renders where it did.

**29. the `finding_id` parse — back to `int()` (the coercing parse the third pass shipped)**
```
RED   13 failed, 37 passed   tests/test_machine_artifacts.py
      FAILED …test_a_finding_id_that_does_not_PARSE_is_refused_not_silently_dropped
             [2.9] [0.0] [True] [False] [10**30] [2**31] [-1] [0] [1e3] [+7]   <- the ten new cases
      FAILED …test_a_json_number_finding_id_does_not_attach_to_a_DIFFERENT_finding[2.9-2]  (and [True-1])
      FAILED …test_an_out_of_range_finding_id_is_refused_BEFORE_the_bytes_are_stored
      FAILED …test_a_multipart_upload_refuses_an_out_of_range_finding_id
GREEN 50 passed
```
The four pre-existing string cases (`abc`, the UUID, `"12.5"`, `[]`) stayed GREEN on both sides — they were
already refused, which is exactly why parametrising only strings masked the hole.

**30. the two new review-surface routes — `can_view_engagement` stripped from both**
```
RED   1 failed, 15 passed   tests/test_scribble_machine_tenancy.py
      FAILED …test_every_engagement_scoped_machine_route_denies_a_foreign_client
             a token for another client was NOT denied on:
             [('scribble_machine.scribble_list_artifacts', 'GET', '…/engagements/1/artifacts', 200),
              ('scribble_machine.scribble_update_artifact', 'POST', '…/engagements/1/artifacts/1', 200)]
GREEN 16 passed
```
The existing tenancy sweep covers the new routes automatically, and its `_build_url` refused to guess a value
for the new `artifact_id` view arg rather than silently 404ing — so it needed a REAL artifact row seeded on
the engagement (`_artifact_on`), which is what makes the 404 above a tenancy refusal and not "no such
artifact". That gate working as designed is why this transcript exists at all.

**31. the id converters — four machine routes back to a bare `<int:>`**
```
RED   1 failed, 66 deselected   tests/test_machine_artifacts.py -k huge_id_in_the_PATH
      assert 500 in (404, 405)
RED   1 failed, 16 passed        tests/test_scribble_machine_tenancy.py
      machine route(s) with an unbounded integer id converter — use api_pat._ID, or a 30-digit path
      segment 500s inside db.get(): [('scribble_machine.scribble_list_artifacts', 'engagement_id', None)]
GREEN 84 passed                  both modules
```
The second one is the drift guard and was proved separately, by reverting ONE route: it names the offending
endpoint and view arg, which is what makes it actionable rather than just red.

<!-- Lifecycle: create + commit this FIRST thing when cutting the branch; keep it current; KEEP it —
     it merges to main with the branch as a durable record (since 2026-07-28; see CLAUDE.md
     "Per-branch plan"). -->
