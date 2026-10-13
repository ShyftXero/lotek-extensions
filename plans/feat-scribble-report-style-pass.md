# Plan: feat/scribble-report-style-pass

- **Branch:** `feat/scribble-report-style-pass`  (worktree: `extensions/.claude/worktrees/report-style`, off `origin/main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose
Make the **DOCX→PDF** pentest report (`report_templates/build_default_docx.py`) look professional instead
of template-generated. The HTML deliverable (`reporting/render_html.py`) is already well-designed and
tested; the DOCX drifted from it (the "colors track render_html" comment is aspirational). This branch
converges the DOCX on the HTML's existing design language. **DOCX-only — `render_html.py` is NOT touched**
(so its color-pinning tests in `test_report_print_media.py` stay green).

## The design spec = the HTML, already shipped
- `.sec-h` section headings: ink-2, uppercase, letter-spaced, grey hairline rule — NOT green.
- `.cover-title`: large ink (40pt), NO green band. `.cover-eyebrow`: small accent-ink caps.
- block labels (`.finding-body .block-label`): small accent-ink **text**, not filled pills.
- CONFIDENTIAL: red keylined badge + handling notice, not centered red text.

## Done
- [x] Cut worktree off origin/main, identity + `commit.gpgsign false`, pulled origin/main (5086076).
- [x] Read guards first: `test_report_card_pills.py` (badge row must keep `roundRect>=3`),
      `test_report_cover_and_toc.py` (CONFIDENTIAL present), `test_report_print_media.py` (HTML accent
      colors — untouched here).

## Remaining
- [ ] S1: DOCX H1 `ACCENT_INK`→`INK2`; keep accent keyline. Open type scale (S4) + body leading (S5).
- [ ] S2: replace full-bleed green cover band with ink title + accent keyline, left-aligned (match HTML cover).
- [ ] S3: `label_chip_xml` green pills → accent-ink small-caps **text** (card + body labels). Keep the
      severity/CVSS/affected **badge** pills (`_pill`) — `roundRect>=3` must hold.
- [ ] S6: CONFIDENTIAL → keylined chip, not centered red. S8: pill text size 7.5pt→8.5pt.
- [ ] Regenerate `default.docx`; run report tests + ruff + pyrefly.
- [ ] EDD: render a LARGE engagement DOCX→PDF via gotenberg before/after; rasterize; iterate until polished.

## Notes / gotchas
- **`main` of the primary `extensions/` checkout is DIRTY** (592 lines uncommitted across 15 files incl.
  render_html.py — NOT mine, looks like an interrupted session). Working in an isolated worktree off
  origin/main avoids it; surfaced to Eli.
- Server PDF export = `exporters.py:_render_pdf` → `convert_docx_to_pdf(_render_docx(...))` → gotenberg.
  The client sees the DOCX cover, not the HTML cover — so DOCX convergence fixes the real artifact.
- `label_chip_xml` feeds BOTH the card section labels AND the body block labels (via render_docx
  `_label_run_xml`) — one change covers both. It's spliced into a RichText body, so the replacement must
  stay a valid `<w:r>` run.
- Severity ramp (`SEVERITY_COLORS`) is a tested shared contract — left alone (S7 dropped).
