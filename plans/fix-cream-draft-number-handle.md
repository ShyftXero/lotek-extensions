# Plan: fix/cream-draft-number-handle

- **Branch:** `fix/cream-draft-number-handle`  (worktree: `.claude/worktrees/ux-cream-number`, off `main`)
- **PR:** not opened yet (the orchestrator opens it)
- **Status:** 🟢 ready to merge — **adversarial review round 2 resolved** (verdict SHIP, 0 blocking,
  1 CONCERN: the PDF-filename assertion sat in a branch the documented pre-PR command never reaches).
  See "Review round 2" below. Round 1 (verdict REPAIR, 3 CONCERNs about *claims*) is resolved above.

## Purpose

Close ext#46 (client-reported, reproduced from a real PAT-driven deliverable session): in cream's
document list a **draft**'s `Number` cell renders as a bare `—`. The link is present and navigable
(`templates/cream/list.html:28` wraps it in `url_for('cream.view_document', doc_id=d.id)`) — the defect
is what the cell *says*: `cream/blueprint.py:122` built `"number": d.number or "—"`, so for an unissued
document the anchor's entire text was a one-character em-dash. No identity, and a near-invisible click
target.

Fix: show a stable, tail-truncated UUIDv7 handle — `draft …b839c91e20` — matching the convention
lotek#336 specifies for a core-owned UUID reference widget, and keep the whole cell clickable.

## Done

- [x] `cream/cream/handles.py` (new) — `uuid_tail()` + `document_handle(number, status, doc_id)`. The
      issued number when there is one; otherwise `<status> …<last 10 of the id>`.
- [x] `cream/cream/blueprint.py` — the list row's `"number"` is now `document_handle(...)`; added
      `_view_meta(doc, editable=…)` (it deduplicates the two `meta` dicts that feed `view.html`) which
      also publishes `handle` alongside the raw `number`.
- [x] `templates/cream/list.html` — the identity cell keeps its link and gains `title="{{ d.id }}"`, so a
      truncated handle is expandable to the full id on hover instead of being a dead end.
- [x] `templates/cream/view.html` — heading is `Invoice draft …b839c91e20` instead of `Invoice (draft)`,
      which was the identical heading on every draft (matters for a **screenshot of the in-app page** —
      corrected claim, see round 1 finding 2: this heading never reaches an export or a print).
- [x] `cream/tests/test_handles.py` (new, 10 tests) + 6 new tests in `cream/tests/test_ui.py` asserting
      the **rendered row**, not just the helper.
- [x] `cream/docs/CREAM.md` — the documents-list section said number is "`—` while draft"; now documents
      the handle, why the tail and not the head, and why bill-to keeps its em-dash.
- [x] Full cream suite green: **152 passed** (`uv run --extra dev pytest`), baseline 136 + 16 new.
      ruff + pyrefly clean on the changed files.

### Review round 1 (added after the adversarial pass)

- [x] **`templates/cream/edit.html` — the same heading fix, on the page a draft is actually opened on.**
      Round 1 finding 1 was right: `list.html`'s only per-row action for a draft is **Edit**, and the
      editor was headed `Invoice draft` / tabbed `CREAM — Edit document` for every draft alike, which is
      the ext#46 complaint one surface in. `_editor_payload` now publishes `handle` (`update_document`
      ignores unknown keys, so it rides along in the PUT-back state harmlessly) and the heading, the tab
      title and the appbar all name the draft. `view.html` gained the same treatment for its `<title>` and
      appbar: **rule — a page showing exactly one document names it, on the page and in the tab.**
- [x] **The export identity — filename + `<title>`.** `handles.export_stem()` (new) gives
      `INV-2026-0001` / `invoice-draft-b839c91e20`, and `render_document_html`/`render_document_pdf` take a
      `name=` kwarg used only for a **standalone** page's `<title>`. Before: every unissued export was
      `document.html`/`document.pdf` titled a bare `Invoice`, so three drafts downloaded as
      `document.pdf`, `document(1).pdf`, `document(2).pdf`. The stem is ASCII and sanitized — it is
      interpolated into a `Content-Disposition` header, so a quote, a semicolon or a CRLF in it would be a
      header-injection primitive, and leading/trailing dots are stripped so a name can neither traverse
      nor land as a hidden file. Numbers are server-minted (`service._next_number`) so this is
      belt-to-braces, not a live hole — but the sanitizing belongs where the name is built.
- [x] **The false verification claim about weasyprint, corrected** — see "weasyprint" under Notes. The
      first report said the PDF branch was exercised; it was not.
- [x] Full cream suite green **in both environments**: 167 passed with `--extra dev --extra pdf`
      (weasyprint present, `export.pdf` takes its 200/`%PDF-` branch) and **167 passed** with
      `--extra dev` alone after `uv sync --extra dev` pruned weasyprint (the 503 branch).
      152 → **167** = 15 new tests this round (6 `test_handles.py`, 6 `test_ui.py`, 3 `test_render.py`).
      `uvx ruff check cream` clean, `pyrefly` 0 errors on the changed files.
- [x] End-to-end probe (`cream.register` + a real test client, two drafts + one issued document, printed
      in "Review round 1 — measured after" below): every surface names its document, and with weasyprint
      installed both drafts export as real `%PDF-` files under distinct names.

## Review round 1 — measured after

Booted through `cream.register` with a real test client (two drafts + one issued invoice), weasyprint
installed:

```
LIST identity cells:   draft …9cbf41ed18 / draft …8c1fa656c4 / INV-2026-0001

draft A (01a010c4-12fb-721d-8790-9a9cbf41ed18)
   view  tab: CREAM — Invoice draft …9cbf41ed18 | h1: Invoice draft …9cbf41ed18
   edit  tab: CREAM — Edit draft …9cbf41ed18    | h1: Invoice draft …9cbf41ed18
   export.html: attachment; filename="invoice-draft-9cbf41ed18.html" | title: Invoice draft …9cbf41ed18
   export.pdf : 200 attachment; filename="invoice-draft-9cbf41ed18.pdf" b'%PDF-'

draft B (01a010c4-1308-7461-a227-3b8c1fa656c4)
   view  tab: CREAM — Invoice draft …8c1fa656c4 | h1: Invoice draft …8c1fa656c4
   edit  tab: CREAM — Edit draft …8c1fa656c4    | h1: Invoice draft …8c1fa656c4
   export.html: attachment; filename="invoice-draft-8c1fa656c4.html" | title: Invoice draft …8c1fa656c4
   export.pdf : 200 attachment; filename="invoice-draft-8c1fa656c4.pdf" b'%PDF-'

issued (01a010c4-1312-7427-bfa5-9b7fbbcca661)
   view  tab: CREAM — Invoice INV-2026-0001 | h1: Invoice INV-2026-0001
   export.html: attachment; filename="INV-2026-0001.html" | title: Invoice INV-2026-0001
   export.pdf : 200 attachment; filename="INV-2026-0001.pdf" b'%PDF-'
```

Before this branch the same run gave `—` / `—` / `INV-2026-0001` in the list, `Invoice (draft)` +
`CREAM — Document` on both view pages, `Invoice draft` + `CREAM — Edit document` on both editors, and
`document.html` / `document.pdf` titled `Invoice` for both drafts.

## Review round 2 — the dead assertion

Verdict **SHIP**, one CONCERN, and it was right: `test_pdf_export_is_a_pdf_or_an_honest_503` is a
two-branch test, and the round-1 filename assertion was parked in the **200** branch — which the
documented pre-PR command never enters, because weasyprint is in cream's `pdf` extra, not `dev`.
Reproduced before touching anything:

```
$ uv run --extra dev python -c "import importlib.util; print(importlib.util.find_spec('weasyprint') is not None)"
False
$ uv run --extra dev --extra pdf python -c "...same..."
True
```

So `export_pdf` could lose its naming entirely and `uv run --extra dev pytest` would still say 167 passed.
An honest docstring warning ("read the branch, not the green dot") is documentation, not a guard.

**Fixed, one step past what was asked.** The reviewer proposed splitting the assertion out behind
`pytest.importorskip("weasyprint")`, which converts a silent green into a visible SKIP. A skip is better
reporting, but it is still not a guard in the environment that gates the PR — and this repo has already
written down that *a skip is not proof anything executed*. So the new
`test_the_pdf_export_names_a_draft_in_its_filename_and_its_metadata_title` **stubs the optional dependency
instead** (`monkeypatch.setattr("cream.blueprint.render_document_pdf", …)`) and therefore RUNS under
`--extra dev`, `--extra pdf`, and anywhere else.

Only the weasyprint call is replaced. Both things it pins are the route's own work, above that seam, and
they are the two halves of the ext#46 fix on this route:

- the `Content-Disposition` filename (`invoice-draft-<tail>.pdf`), and
- the `name` handed to the renderer — the handle weasyprint writes into the PDF's **metadata title**,
  i.e. what a PDF reader shows in its window and its recent-files list.

The second one is why stubbing beats `importorskip` on substance as well as on reporting: that title is
**not observable from the returned bytes** without decompressing the PDF's object stream (the reviewer had
to do exactly that by hand to confirm `/Title (Invoice draft ZZTAILZZ)`). A real-weasyprint test asserting
only the filename — the fix as prescribed — would stay green while the metadata title regressed to a bare
`Invoice`. The stub catches it; measured below.

`test_pdf_export_is_a_pdf_or_an_honest_503` is otherwise left exactly as it was (real PDF **or** honest
503, never HTML under a `.pdf` name); it just no longer carries a naming assertion it cannot reach, and
its docstring says where that guard went.

Counts after: **168 passed** with `--extra dev` (was 167) and **168 passed** with `--extra dev --extra pdf`
— the same number both ways, which is the point: nothing is now conditional on the optional dependency.

## Remaining

- [ ] Nothing for this branch. Follow-ons, deliberately NOT done here:
  - **A mounted test in lotek core** (`lotek/tests/test_cream_extension.py`) — this repo's suite proves
    cream's own logic with a stub host; a mounted assertion lives in the other repo and is out of this
    track's scope.
  - **The exported/printed DOCUMENT still carries no draft identity in its body.** `render.py` omits the
    `<div class="num">` when there is no number, so an unissued export's document reads `Invoice` with the
    status pill and nothing else. That is a **deliberate boundary, not an oversight** (round 1 finding 2
    asked for it to be fixed or declared): the filename and the `<title>` are app-side naming, but the
    body is the copy a client receives, and an id tail printed where the invoice number goes reads as an
    invoice number. Pinned as a boundary by
    `test_the_exported_document_body_does_not_print_an_id_as_a_number` +
    `test_a_name_titles_the_page_without_printing_itself_on_the_document`, so a later change that decides
    otherwise has to do it on purpose. If the owner wants a `DRAFT — not an invoice · …b839c91e20` line in
    the body, that is a deliverable-content decision and a separate change.
  - **Move onto lotek#336's widget when it lands.** That issue is open — core has `combobox.js` and
    `data-table.js` but no UUID-reference widget yet, so there is nothing to consume today.
    `cream/handles.py` follows #336's stated convention (tail, leading `…`, full id on hover) precisely so
    the swap is mechanical, and `docs/CREAM.md` says so. #336 also proposes a guard that fails a build
    when an extension hand-rolls a UUID abbreviation — this call site is what that guard should point at.

## Notes / gotchas

- **Tail, never prefix.** UUIDv7's leading 48 bits are a millisecond timestamp; lotek#336 measured five
  consecutively created ids sharing their first **23** characters (1 distinct first-8 of 5, 5 distinct
  last-8 of 5). A "short id" taken from the front does not discriminate, and it fails *silently*. RED
  transcript 2 below reproduces it: head-truncating collapsed 5 freshly minted ids to **1** handle.
- **Length differs from #336's default, on purpose.** #336 defaults `data-short-len=6`, where the short
  id sits beside a friendly name (`Acme Corp · …841044`) and only breaks ties. Here the handle is the
  whole content of the identity column, so it carries all of the identity; 10 is what the client asked
  for and what ext#46 specifies, and #336 makes the length a per-call-site knob for exactly this.
- **The leading word is the STATUS, not the literal "draft".** `service.void()` accepts a draft, so an
  unnumbered document is not necessarily a draft — `void …b839c91e20`. RED transcript 4 pins that.
- **`bill_to_name or "—"` was left alone** (the next line in the same row, flagged in the issue as worth
  checking). A blank bill-to is a missing *field*; the em-dash reads correctly as "empty" and, unlike the
  number cell, it is not the row's link. Printing an id tail under a column headed "Bill to" would invent
  an identity for a client record that may not exist yet. Same reasoning leaves `render.py`'s two
  `or "—"` fallbacks (issuer company name, engagement title) untouched.
- **The machine/JSON API still reports `number: null` for a draft.** The handle is a *display* affordance;
  a machine reader must not be handed a synthesized identifier it cannot look anything up by.
- The triage repro scripts (`repro_report.py`, `repro_checklist_ui.py`, `pdf_probe.py`) are all scribble
  report-rendering probes — none touches cream, so there was no script to reuse as an acceptance check
  here. The `tests/test_ui.py` row assertions are the acceptance check.
- `pytest` in this subproject already sets `-q` in `addopts`, so adding another `-q` gives `-qq` and
  **suppresses the pass/fail count line**. Run plain `uv run --extra dev pytest` to see counts.
- 🔴 **weasyprint is in cream's `pdf` extra, NOT `dev` — so `uv run --extra dev pytest` does not test PDF
  rendering.** The first version of this plan/report claimed "weasyprint IS installed in this venv, so
  `test_pdf_export_is_a_pdf_or_an_honest_503` took its 200/`%PDF-` branch". **That was false** (round 1
  finding 3, correctly measured by the reviewer): `uv run --extra dev python -c "import weasyprint"` →
  `ModuleNotFoundError`, and the test silently took its **503** branch. The test is a two-branch guard, so
  it is green either way — which is exactly how the wrong claim survived. Measured, not assumed, this round:
  temporarily replacing the 503 branch with `raise AssertionError("PROBE: took the 503 branch")` FAILED
  under `--extra dev` and PASSED under `--extra dev --extra pdf`. Use
  `uv run --extra dev --extra pdf pytest` when you care about PDF output, and read the branch rather than
  the green dot.
  - Second-order gotcha: **`uv run --extra dev` does NOT prune weasyprint** once a `--extra pdf` run has
    installed it — the venv keeps it and every later `--extra dev` run quietly takes the 200 branch.
    `uv sync --extra dev` is what removes it. So "which branch ran" is a property of the venv's history,
    not of the command line you can see in the transcript.

## Red-then-green

Four induced breakages, each restored afterwards. Commands run from `cream/`; output ANSI-stripped and
trimmed to the summary lines.

### 1. The reported defect itself — put back `"number": d.number or "—"` in `blueprint.dashboard`

```
$ uv run --extra dev pytest -p no:warnings -rf --tb=line tests/test_ui.py
E   AssertionError: assert '—' == 'draft …5b195e0361'
E   AssertionError: assert '—' == 'void …b02d7f81bf'
FAILED tests/test_ui.py::test_a_draft_row_names_itself_with_a_tail_truncated_handle - AssertionError: assert '—' == 'draft …5b195e0361'
FAILED tests/test_ui.py::test_a_voided_draft_is_not_labelled_a_draft - AssertionError: assert '—' == 'void …b02d7f81bf'
2 failed, 15 passed in 1.77s
```

restored →

```
$ uv run --extra dev pytest -p no:warnings tests/test_ui.py
17 passed in 1.75s
```

### 2. Head-truncate instead of tail — `uuid_tail` returns `f"{text[:length]}{ELLIPSIS}"`

The interesting line is `assert 1 == 5`: five freshly minted UUIDv7s produced **one** handle between
them. That is the silent failure mode, measured.

```
$ uv run --extra dev pytest -p no:warnings -rf --tb=line tests/test_handles.py tests/test_ui.py
FAILED tests/test_handles.py::test_the_tail_is_marked_as_a_fragment - AssertionError: assert '01a00ff7-8…' == '…b839c91e20'
FAILED tests/test_handles.py::test_the_handle_discriminates_ids_that_share_23_leading_characters - AssertionError: two documents created in the same millisecond got the same ...
FAILED tests/test_handles.py::test_freshly_minted_uuid7s_all_get_distinct_handles - AssertionError: assert 1 == 5
FAILED tests/test_handles.py::test_an_unissued_document_gets_its_status_and_its_id_tail - AssertionError: assert 'draft 01a00ff7-8…' == 'draft …b839c91e20'
FAILED tests/test_handles.py::test_a_blank_number_counts_as_unissued - AssertionError: assert 'draft 01a00ff7-8…' == 'draft …b839c91e20'
FAILED tests/test_handles.py::test_the_label_is_the_status_not_the_literal_word_draft - AssertionError: assert 'void 01a00ff7-8…' == 'void …b839c91e20'
FAILED tests/test_ui.py::test_a_draft_row_names_itself_with_a_tail_truncated_handle - AssertionError: assert 'draft 01a010aa-b…' == 'draft …606e9bdd49'
FAILED tests/test_ui.py::test_a_voided_draft_is_not_labelled_a_draft - AssertionError: assert 'void 01a010aa-b…' == 'void …848f42a32a'
FAILED tests/test_ui.py::test_the_view_page_heading_names_an_unissued_document - AssertionError: assert 'Invoice draft 01a010aa-b…' == 'Invoice draft …60530...
9 failed, 18 passed in 1.91s
```

restored →

```
$ uv run --extra dev pytest -p no:warnings tests/test_handles.py tests/test_ui.py
27 passed in 1.40s
```

### 3. Revert the view heading to `{{ meta.number or '(draft)' }}`

```
$ uv run --extra dev pytest -p no:warnings -rf --tb=line tests/test_ui.py
E   AssertionError: assert 'Invoice (draft)' == 'Invoice draft …033b090b05'
FAILED tests/test_ui.py::test_the_view_page_heading_names_an_unissued_document - AssertionError: assert 'Invoice (draft)' == 'Invoice draft …033b090b05'
1 failed, 16 passed in 1.80s
```

restored →

```
$ uv run --extra dev pytest -p no:warnings tests/test_ui.py
17 passed in 1.44s
```

### 4. Hardcode the label — `label = "draft"` instead of the status

```
$ uv run --extra dev pytest -p no:warnings -rf --tb=line tests/test_handles.py tests/test_ui.py
FAILED tests/test_handles.py::test_the_label_is_the_status_not_the_literal_word_draft - AssertionError: assert 'draft …b839c91e20' == 'void …b839c91e20'
FAILED tests/test_handles.py::test_a_missing_id_yields_no_fake_identifier - AssertionError: assert 'draft' == '…'
FAILED tests/test_ui.py::test_a_voided_draft_is_not_labelled_a_draft - AssertionError: assert 'draft …fee4690cc3' == 'void …fee4690cc3'
3 failed, 24 passed in 1.72s
```

restored →

```
$ uv run --extra dev pytest -p no:warnings tests/test_handles.py tests/test_ui.py
27 passed in 2.11s
```

### Whole suite, restored

```
$ uv run --extra dev pytest -p no:warnings
152 passed in 6.19s
```

## Red-then-green — review round 1

Four more induced breakages for the four guards added this round, each restored afterwards. Commands run
from `cream/`; output ANSI-stripped and trimmed.

### 5. Revert the editor heading + both pages' tab titles (`<h1>{{ doc.kind|capitalize }} draft</h1>`, `CREAM — Edit document`, `CREAM — Document`)

The exact state the reviewer measured: two editors, one heading.

```
$ uv run --no-sync pytest -p no:warnings --tb=long -k "editor_heading_names or four_distinguishable" tests/test_ui.py
>       assert headings == [f"Invoice draft …{first['id'][-10:]}",
E       AssertionError: assert ['Invoice dra...nvoice draft'] == ['Invoice dra... …25545d41df']
E         At index 0 diff: 'Invoice draft' != 'Invoice draft …831c0488f7'
>               assert f"…{doc['id'][-10:]}" in title, (path, title)
E               AssertionError: ('/cream/documents/01a010c0-7753-732c-a4af-d9ed6e51d2c3', 'CREAM — Document')
E               assert '…ed6e51d2c3' in 'CREAM — Document'
2 failed, 20 deselected in 0.95s
```

restored →

```
$ uv run --no-sync pytest -p no:warnings tests/test_ui.py
22 passed in 5.23s
```

### 6. Revert the export filename to `(doc.number or "document")`, then (separately) the `name=` kwarg

Two breaks, because the filename assertion fires first and would otherwise hide the `<title>` guard.

```
$ uv run --no-sync pytest -p no:warnings --tb=long -k "draft_export_names or issued_export_is_still" tests/test_ui.py
E       AssertionError: assert 'document.html' == 'invoice-draf...7e496bba.html'
E         - invoice-draft-4b7e496bba.html
E         + document.html
1 failed, 1 passed, 20 deselected in 0.80s      # the ISSUED export test still passes — unchanged behaviour
```

then filename restored, `name=` still dropped — isolating the `<title>`:

```
$ uv run --no-sync pytest -p no:warnings --tb=long -k "draft_export_names" tests/test_ui.py
E       AssertionError: assert 'Invoice' == 'Invoice draft …4854c09905'
1 failed, 21 deselected in 0.51s
```

restored →

```
$ uv run --no-sync pytest -p no:warnings tests/test_ui.py
22 passed in 5.84s
```

### 7. `export_stem` returns the raw display handle (no sanitizing, no ASCII coercion)

The one line that would look harmless in review — "the handle is already a name, reuse it" — and it puts
`"` and `\r\n` from a number straight into a `Content-Disposition` header.

```
$ uv run --no-sync pytest -p no:warnings -rf --tb=line tests/test_handles.py tests/test_ui.py
E   AssertionError: assert 'draft …b839c91e20' == 'invoice-draft-b839c91e20'
E   AssertionError: assert False                      # .isascii() / charset
E   assert 'INV-1"\r\nX-Evil: 1' == 'INV-1-X-Evil-1'
E   AssertionError: assert 'draft' == 'invoice-draft'
E   AssertionError: assert 'draft …1ca7387e36.html' == 'invoice-draf...a7387e36.html'
E   AssertionError: assert 'draft …ef1572e384.pdf' == 'invoice-draft-ef1572e384.pdf'
6 failed, 32 passed in 5.24s
```

restored →

```
$ uv run --no-sync pytest -p no:warnings tests/test_handles.py tests/test_ui.py
38 passed in 6.16s
```

### 8. Let the `name` leak into the document body — `<div class="num">{plain(view.number or name)}</div>`

The boundary guard: naming the file and the tab is app-side, printing an id where the invoice number goes
is not.

```
$ uv run --no-sync pytest -p no:warnings -rf --tb=line tests/test_render.py tests/test_ui.py
E   assert 'b839c91e20' not in '<body>\n<di...body></html>'
E   assert '33bd46a8ed' not in '<body>\n<di...body></html>'
FAILED tests/test_render.py::test_a_name_titles_the_page_without_printing_itself_on_the_document
FAILED tests/test_ui.py::test_the_exported_document_body_does_not_print_an_id_as_a_number
2 failed, 43 passed in 5.39s
```

restored →

```
$ uv run --no-sync pytest -p no:warnings tests/test_render.py tests/test_ui.py
45 passed in 6.47s
```

### 8b. Drop the dot-stripping from the filename stem (`.strip("-.")` -> `.strip("-")`)

```
$ uv run --no-sync pytest -p no:warnings -rf --tb=line tests/test_handles.py
E   AssertionError: assert '..-..-etc-passwd' == 'etc-passwd'
FAILED tests/test_handles.py::test_a_dotted_number_cannot_produce_a_traversal_or_a_hidden_file
1 failed, 16 passed in 0.08s
```

restored →

```
$ uv run --no-sync pytest -p no:warnings tests/test_handles.py
17 passed in 0.04s
```

### 9. Which branch the PDF test takes — the round-1 finding-3 measurement

Not a guard break; a measurement of the *environment*, because the claim that was wrong last round was
about the environment and not about the code. The 503 branch was temporarily replaced with a raise.

```
$ uv sync --extra dev                       # prunes weasyprint (13 packages removed)
$ uv run --no-sync python -c "import weasyprint"
weasyprint ABSENT: No module named 'weasyprint'
$ uv run --no-sync pytest -p no:warnings -rf --tb=line -k test_pdf_export
E   AssertionError: PROBE: took the 503 branch, PDF rendering NOT exercised
1 failed, 165 deselected in 0.95s

$ uv run --extra dev --extra pdf pytest -p no:warnings -rf --tb=line -k test_pdf_export
1 passed, 165 deselected in 5.37s          # 200 + %PDF- + filename invoice-draft-<tail>.pdf
```

probe removed, both environments run the whole suite green:

```
$ uv run --extra dev --extra pdf pytest -p no:warnings      # weasyprint present -> 200 branch
167 passed in 12.72s
$ uv sync --extra dev && uv run --no-sync pytest -p no:warnings   # weasyprint absent -> 503 branch
167 passed in 10.43s
```

## Red-then-green — review round 2

One guard added, so two induced breakages — one per half of the fix it protects. Both run under the
**default** `--extra dev` command, which is the whole point of the change: the same reverts previously
left that command green. Output ANSI-stripped and trimmed.

### 7. Revert `export_pdf` to its pre-ext#46 naming (`name = (doc.number or "document") + ".pdf"`, no `name=` kwarg)

```
$ uv run --extra dev pytest tests/test_ui.py
>       assert _attachment_filename(res) == f"invoice-draft-{tail}.pdf"
E       AssertionError: assert 'document.pdf' == 'invoice-draft-215f88c73a.pdf'
tests/test_ui.py:261: AssertionError
FAILED tests/test_ui.py::test_the_pdf_export_names_a_draft_in_its_filename_and_its_metadata_title
1 failed, 22 passed in 5.04s
```

### 8. Restore the filename, drop ONLY the `name=document_handle(...)` kwarg — isolating the metadata title

```
$ uv run --extra dev pytest tests/test_ui.py
FAILED tests/test_ui.py::test_the_pdf_export_names_a_draft_in_its_filename_and_its_metadata_title
  - AssertionError: assert [''] == ['draft …049baf1717']
1 failed, 22 passed in 5.88s
```

Each half fails on its own, so neither assertion is riding on the other. Restored (`git diff` on
`cream/cream/blueprint.py` empty) →

```
$ uv run --extra dev pytest
168 passed in 25.76s

$ uv run --extra dev --extra pdf pytest
168 passed in 31.40s

$ uvx ruff check tests/test_ui.py
All checks passed!

$ uv run --extra dev pyrefly check tests/test_ui.py
INFO 0 errors
```
