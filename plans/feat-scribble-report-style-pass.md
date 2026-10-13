# Plan: feat/scribble-report-style-pass

- **Branch:** `feat/scribble-report-style-pass`  (worktree: `extensions/.claude/worktrees/report-style`, off `origin/main`)
- **PR:** not opened yet
- **Status:** 🟢 ready to merge (local verify done; see Evals)

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

## Done (continued)
- [x] S1: H1 `ACCENT_INK`→`INK2`, UPPERCASE+tracked, accent keyline (`_para_border`). Type scale opened
      (Title 28, H1 15, H2 14, H3 12), leading 1.12→1.32.
- [x] S2: cover band removed → ink title + accent keyline + accent-ink eyebrow; Client/Company deduped;
      window row guarded (no dangling en-dash); left-aligned.
- [x] S3: `label_chip_xml` green pill → accent-ink UPPERCASE text; badge pills kept (`roundRect>=3` holds).
- [x] S6: CONFIDENTIAL → left-aligned, red keyline classification footer (pushed ~⅔ down). S8: pill 7.5→8.5pt.
- [x] Bonus fix (polish): Affected Hosts / References were a green run-on (bare `<li>`); now bulleted.
- [x] Regenerate `default.docx`; targeted report tests green; pinned ruff clean.
- [x] EDD render: large (65-finding) UKG-shaped engagement → DOCX → gotenberg PDF, rasterized + eyeballed.

## Evals
- Grader: visual before/after of a large synthetic engagement rendered via the lotek-gotenberg image
  (real Inter/JetBrains Mono). Before = original prod UKG PDF (~/ukg-pdf-review); after = /tmp/style.
- Deltas: cover no longer a green band + dangling "Assessment window –" + duplicate Client/Company;
  section headings restrained ink keyline not loud green; block labels text not pills; Affected Hosts
  bulleted not a green run-on. Deterministic: `test_report_card_pills` (badge pills kept) +
  new `test_docx_affected_hosts_render_as_a_bulleted_list` (red on bare `<li>`, green with `<p>` wrap).

## Remaining (out of this style scope — flagged, not done)
- [ ] Rollup tables (`_append_rollups`, mounted-only) dominate the real report; consider appendix placement.
- [ ] Coverage checklist renders a wall of `[pending]` when 100% open — summarize instead.
- [ ] Affected Hosts list + native Affected Assets grid still duplicate the host set (both now clean).

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
