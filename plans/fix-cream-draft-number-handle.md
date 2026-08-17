# Plan: fix/cream-draft-number-handle

- **Branch:** `fix/cream-draft-number-handle`  (worktree: `.claude/worktrees/ux-cream-number`, off `main`)
- **PR:** not opened yet (the orchestrator opens it)
- **Status:** 🟢 ready to merge

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
      which was the identical heading on every draft (matters for a screenshot or a printed page).
- [x] `cream/tests/test_handles.py` (new, 10 tests) + 6 new tests in `cream/tests/test_ui.py` asserting
      the **rendered row**, not just the helper.
- [x] `cream/docs/CREAM.md` — the documents-list section said number is "`—` while draft"; now documents
      the handle, why the tail and not the head, and why bill-to keeps its em-dash.
- [x] Full cream suite green: **152 passed** (`uv run --extra dev pytest`), baseline 136 + 16 new.
      ruff + pyrefly clean on the changed files.

## Remaining

- [ ] Nothing for this branch. Follow-ons, deliberately NOT done here:
  - **A mounted test in lotek core** (`lotek/tests/test_cream_extension.py`) — this repo's suite proves
    cream's own logic with a stub host; a mounted assertion lives in the other repo and is out of this
    track's scope.
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
