"""ext#39 — the "Findings by severity" block has to keep its colour on paper.

Measured in a real Chromium under ``emulate_media(media="print")``, because this defect is invisible to a
string check of the CSS and invisible to a screen render: the computed style was

    bg=rgb(194, 65, 12)  color=rgb(255, 255, 255)  printColorAdjust=economy

``economy`` is the permissive default, and Chrome's print path drops background fills at ``economy``
whenever "Background graphics" is unchecked — which is the print dialog's DEFAULT and therefore what the
client got: a blank severity bar with white numerals on white paper, and a legend reduced to bare columns
of digits while the rest of the document kept its colour (text and borders are painted; backgrounds are
not).

The document renders from a ``file://`` URL — the report is self-contained by contract (inline CSS/JS, no
external hosts), so no server is needed to measure it, and asserting that it works this way also keeps the
"one deliverable file" contract honest.

SKIP-CLEAN: no Playwright / no usable Chromium -> skip, never fail (docs/RAILS.md).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from scribble import checklists as C
from scribble.content import schema
from scribble.enums import Severity
from scribble.models import (
    ChecklistTemplate,
    Client,
    Engagement,
    EngagementFinding,
    FindingGroup,
)
from scribble.reporting import build_report_context
from scribble.reporting.render_html import _CSS, render_report_html

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - exercised by whichever lane lacks the dep
    sync_playwright = None

# The LIGHT ramp (``:root`` and, since ext#39, ``@media print`` too), as Chromium reports it.
LIGHT_HIGH = "rgb(194, 65, 12)"      # --sev-high  #c2410c
LIGHT_CRITICAL = "rgb(179, 38, 30)"  # --sev-critical #b3261e
LIGHT_INK = "rgb(16, 32, 46)"        # --ink on paper: #10202e
DARK_HIGH = "rgb(239, 138, 68)"      # --sev-high in the dark ramp: #ef8a44
DARK_INK = "rgb(231, 238, 245)"      # --ink in the dark ramp: #e7eef5
# The ACCENT family, which the print block did not pin at first (see the accent tests below).
LIGHT_ACCENT_INK = "rgb(10, 91, 61)"     # --accent-ink on paper: #0a5b3d  -> 8.3:1 on white
DARK_ACCENT_INK = "rgb(126, 224, 188)"   # --accent-ink in the dark ramp: #7ee0bc -> 1.6:1 on white
LIGHT_ACCENT = "rgb(15, 122, 82)"        # --accent on paper: #0f7a52

# The same two ramp colours as 8-bit RGB, for counting pixels on a rasterized page.
PX_CRITICAL = (179, 38, 30)
PX_HIGH = (194, 65, 12)


def _pdf_pages_rgb(pdf: Path) -> list[bytes]:
    """EVERY page of ``pdf`` as raw RGB bytes, via poppler's ``pdftoppm`` writing P6 PPMs.

    Pixels, not PDF operators. Reading back the content stream's fill operators (``r g b rg``) looks
    tempting and is WRONG for this defect: a severity colour also appears there as the finding card's
    left border and as severity-coloured text, both of which print with background graphics off — so an
    operator-level check passes whether or not the bug is present (measured: it did). What distinguishes
    the two is how much of the page is actually painted in that colour.

    Every page, not page 1: since ext#43 the deliverable opens on a cover page and a table of contents, so
    the severity bar is on page 3. Measuring a fixed page number would have quietly turned this guard into
    an assertion about a title page — the paginated position of a widget is not what it is testing.

    PPM is parsed by hand so this needs no image library — only poppler, which the caller skips without.
    """
    stem = pdf.with_suffix("")
    subprocess.run(  # noqa: S603 - fixed argv, path from tmp_path
        ["pdftoppm", "-r", "72", str(pdf), str(stem)],
        check=True,
        capture_output=True,
    )
    pages = sorted(pdf.parent.glob(f"{stem.name}-*.ppm"))
    assert pages, f"pdftoppm produced no pages for {pdf.name}"
    out: list[bytes] = []
    for ppm in pages:
        magic, _dims, _maxval, pix = ppm.read_bytes().split(b"\n", 3)
        assert magic.strip() == b"P6", f"unexpected raster format {magic!r}"
        out.append(pix)
    return out


def _count_colour(pages: list[bytes], target: tuple[int, int, int], tol: int = 12) -> int:
    """Pixels within ``tol`` of ``target`` across every page — tolerant of the rasterizer's antialiasing."""
    tr, tg, tb = target
    return sum(
        1
        for pix in pages
        for i in range(0, len(pix) - 2, 3)
        if abs(pix[i] - tr) < tol and abs(pix[i + 1] - tg) < tol and abs(pix[i + 2] - tb) < tol
    )


def _block(text: str) -> dict:
    return schema.doc_from_text(text)


@pytest.fixture(scope="module")
def browser():
    if sync_playwright is None:
        pytest.skip("playwright is not installed; skipping print-media checks (skip-clean)")
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - any launch failure -> skip-clean
            pytest.skip(f"no usable Chromium runtime ({exc}); skipping print-media checks (skip-clean)")
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def report_page(browser, session_factory, tmp_path):
    """A rendered report open in a real browser: two severities (so the bar has segments), a host (so the
    findings index renders a ``.sev-tag``), and the metrics row."""
    with session_factory() as db:
        client = Client(name="TeamsPlus")
        db.add(client)
        db.flush()
        eng = Engagement(
            name="Print Media Assessment", client_id=client.id, company_name="TeamsPlus",
            scope_type="web-app",
        )
        grp = FindingGroup(engagement=eng, name="Web Application", order_index=0)
        EngagementFinding(
            engagement=eng, group=grp, title="Reflected XSS", severity=Severity.high, order_index=0,
            target_host="portal.teamsplus.example", cvss_score=7.4,
            content_json={"description": _block("Reflected XSS in the search parameter.")},
        )
        EngagementFinding(
            engagement=eng, group=grp, title="Exposed Admin Console", severity=Severity.critical,
            order_index=1, content_json={"description": _block("Unauthenticated admin console.")},
        )
        db.add_all([eng, grp])
        db.commit()
        eid = eng.id
    with session_factory() as db:
        html = render_report_html(build_report_context(db.get(Engagement, eid)))
    path = tmp_path / "report.html"
    path.write_text(html, encoding="utf-8")

    page = browser.new_page(viewport={"width": 1400, "height": 1000})
    page.goto(path.as_uri(), wait_until="load")
    try:
        yield page
    finally:
        page.close()


def _rule_tokens(css: str, opening: str, token_re: re.Pattern[str]) -> set[str]:
    """The custom-property names declared by the CSS rule whose text starts with ``opening``.

    Brace-matched rather than regex-sliced because one of the palette rules is a bare ``:root`` wrapping a
    nested ``@media`` block; a non-greedy match to the first ``}`` would read half of it.
    """
    at = css.find(opening)
    if at < 0:
        return set()
    start = css.index("{", at) + 1
    depth, i = 1, start
    while depth and i < len(css):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
        i += 1
    return set(token_re.findall(css[start : i - 1]))


def _computed(page, selector: str, prop: str) -> str:
    return page.evaluate(
        """([sel, prop]) => {
             const el = document.querySelector(sel);
             if (!el) return "MISSING:" + sel;
             const cs = getComputedStyle(el);
             return cs[prop] || cs.getPropertyValue(prop) || "";
           }""",
        [selector, prop],
    )


# ── the fills survive "Background graphics: off" ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "selector",
    [
        ".sevbar .seg",      # the severity bar segment: #fff numerals ON the fill
        ".sevlegend .sw",     # a legend swatch: nothing BUT a background
        ".sev-tag",           # findings-index severity tag (color-mix background)
        ".sev-badge",         # per-finding severity badge
        ".metrics",           # the grid whose 1px gap IS the rule between tiles
        ".metric",            # ...and the tiles painted over it
    ],
)
def test_meaningful_fills_are_marked_print_exact(report_page, selector):
    report_page.emulate_media(media="print")
    assert _computed(report_page, selector, "printColorAdjust") == "exact", (
        f"{selector} would lose its background fill when printed with background graphics off"
    )


def test_the_paper_background_is_not_forced(report_page):
    """The scoping half of the fix: ``print-color-adjust: exact`` must NOT be blanket on ``*`` — that
    would force the page's own background onto the paper (and burn a client's toner)."""
    report_page.emulate_media(media="print")
    for selector in ("body", "main.wrap", ".finding"):
        assert _computed(report_page, selector, "printColorAdjust") == "economy", (
            f"{selector} was forced to exact; the fix must stay scoped to meaningful colour"
        )


# ── the printed ramp is the LIGHT ramp, whatever the viewer's colour scheme ───────────────────────


def test_print_uses_the_light_severity_ramp_from_a_dark_viewer(report_page):
    """The second, smaller defect in the same block: ``@media print`` overrode ``--bg``/``--ink``/… for
    paper but not ``--sev-*``, so a viewer in dark mode printed the DARK ramp onto a white sheet."""
    report_page.emulate_media(media="print", color_scheme="dark")
    high = _computed(report_page, ".sevbar .seg.sev-high", "backgroundColor")
    assert high == LIGHT_HIGH, f"printed the wrong severity ramp: {high} (dark ramp is {DARK_HIGH})"
    critical = _computed(report_page, ".sevbar .seg.sev-critical", "backgroundColor")
    assert critical == LIGHT_CRITICAL


def test_print_uses_the_paper_palette_from_a_dark_viewer(report_page):
    """Found while fixing ext#39, and worse than the reported half: the ENTIRE ``@media print`` palette
    override was losing the cascade for a dark-mode viewer. The dark palette is declared on
    ``:root:not([data-theme="light"])`` (0-2-0) and the print block used a plain ``:root`` (0-1-0), so
    printing from a dark-mode browser computed the dark near-white ink — text that all but vanishes on
    unpainted white paper, for the whole document rather than one widget.

    Asserting ``body`` alone was NOT enough and is why this test is worth reading twice: the first
    version of the fix pinned ``--bg``/``--surface``/``--ink``/``--line``/``--sev-*`` and silently left
    the ``--accent*`` family on its dark values, which this test could not see because ``body`` does not
    use it. The cover's client name did (see the accent tests below). A palette guard has to name a
    colour from EVERY family the override claims to cover."""
    report_page.emulate_media(media="print", color_scheme="dark")
    ink = _computed(report_page, "body", "color")
    assert ink == LIGHT_INK, f"printed the dark ink {ink} (paper ink is {LIGHT_INK}) onto white paper"
    assert _computed(report_page, "body", "backgroundColor") == "rgb(255, 255, 255)"
    eyebrow = _computed(report_page, ".cover-eyebrow", "color")
    assert eyebrow == LIGHT_ACCENT_INK, (
        f"the CLIENT NAME on the printed title page came out {eyebrow}; the dark accent "
        f"{DARK_ACCENT_INK} on white paper is 1.6:1 — page 1 of the deliverable is unreadable"
    )


@pytest.mark.parametrize(
    "selector",
    [
        ".cover-eyebrow",              # the client name on the printed title page
        ".fm-block h3",                # "ENGAGEMENT OVERVIEW" / "SCOPE AND LIMITATIONS"
        ".mth-k",                      # every methodology phase name
        ".finding-body .block-label",  # "Description" / "Remediation" on every finding card
    ],
)
def test_accent_text_prints_in_the_paper_accent_from_a_dark_viewer(report_page, selector):
    """The ``--accent*`` half of the same defect, and the one the ``body``-only assertion above missed.

    Every one of these elements takes its colour from ``--accent-ink``. With the family left off the
    print palette, a dark-mode viewer printed ``#7ee0bc`` (pale mint) on white paper — measured at
    1.58:1, against 8.3:1 for the paper accent. Four separate widgets, so a future edit that pins the
    family for one of them and not the rest still fails."""
    report_page.emulate_media(media="print", color_scheme="dark")
    got = _computed(report_page, selector, "color")
    assert got == LIGHT_ACCENT_INK, (
        f"{selector} printed {got} instead of the paper accent {LIGHT_ACCENT_INK} — the dark accent "
        f"{DARK_ACCENT_INK} on white paper is 1.6:1"
    )


def test_the_print_palette_pins_EVERY_token_the_dark_theme_overrides(report_page):
    """The drift guard for the whole class: whatever the dark theme redefines, the print block must
    redefine too.

    Hermetic (parses ``_CSS``; it takes ``report_page`` only to sit with the tests it protects) and
    deliberately name-blind — it does not know what ``--accent-wash`` is for. That is the point: the
    ``@media print`` palette was written by listing the families someone thought of, which is exactly how
    ``--accent``/``--accent-ink``/``--accent-wash`` were left on their dark values while the docs and the
    test above claimed the whole paper palette was pinned. A token added to the dark theme from now on
    fails here until it is pinned for paper as well."""
    tokens = re.compile(r"(--[a-z0-9-]+)\s*:")
    dark_media = _rule_tokens(_CSS, ':root:not([data-theme="light"]) {', tokens)
    dark_stamp = _rule_tokens(_CSS, '\n:root[data-theme="dark"] {', tokens)
    printed = _rule_tokens(_CSS, ':root:not([data-theme="dark"]), :root[data-theme="dark"] {', tokens)

    assert dark_media and dark_stamp and printed, "the palette rules moved; fix this guard's selectors"
    assert dark_media == dark_stamp, (
        "the two dark declarations disagree: "
        f"{sorted(dark_media ^ dark_stamp)} is declared by only one of them"
    )
    missing = sorted(dark_stamp - printed)
    assert not missing, (
        f"the @media print palette does not pin {missing} — a dark-mode viewer prints those dark values "
        "onto white paper"
    )


def test_a_dark_template_still_prints_on_paper_colours(report_page, session_factory, tmp_path, browser):
    """A template that FORCES the dark theme (``<html data-theme="dark">``) must still print light: the
    sheet is white either way. Covers the ``:root[data-theme="dark"]`` half of the print override."""
    with session_factory() as db:
        eng = Engagement(name="Dark Template", company_name="TeamsPlus")
        grp = FindingGroup(engagement=eng, name="Web Application", order_index=0)
        EngagementFinding(
            engagement=eng, group=grp, title="Reflected XSS", severity=Severity.high, order_index=0,
            content_json={"description": _block("Reflected XSS in the search parameter.")},
        )
        db.add_all([eng, grp])
        db.commit()
        eid = eng.id
    with session_factory() as db:
        html = render_report_html(build_report_context(db.get(Engagement, eid)), template="dark")
    assert 'data-theme="dark"' in html  # the template really did force it
    path = tmp_path / "dark-report.html"
    path.write_text(html, encoding="utf-8")
    page = browser.new_page()
    try:
        page.goto(path.as_uri(), wait_until="load")
        page.emulate_media(media="print")
        assert _computed(page, "body", "color") == LIGHT_INK
        assert _computed(page, ".sevbar .seg.sev-high", "backgroundColor") == LIGHT_HIGH
    finally:
        page.close()


def test_the_compliance_badges_print_readably_from_a_dark_viewer(session_factory, tmp_path, browser):
    """The worst case the missing ``--accent*`` pin produced, because ``print-color-adjust: exact`` forces
    the fill to actually paint: ``.ck-badge.ck-satisfied`` is ``#fff`` text ON ``--accent-ink``, so on
    paper from a dark-mode browser it printed white-on-pale-mint (1.58:1) while its ``.ck-deficient``
    sibling — whose ``--sev-high`` background WAS pinned — printed correctly. That inconsistency is the
    omission's fingerprint, and it lands on a section titled "Compliance Attestation"."""
    with session_factory() as db:
        eng = Engagement(name="Attestation", company_name="TeamsPlus", scope_type="external")
        db.add(eng)
        db.flush()
        comp = C.assign_template(
            db, eng, db.query(ChecklistTemplate).filter_by(slug="pci-dss-segmentation").one()
        )
        comp.items[0].status = "pass"  # -> bucket "satisfied", the accent-backed badge
        comp.items[1].status = "fail"  # -> bucket "deficient", the --sev-high one
        db.commit()
        eid = eng.id
    with session_factory() as db:
        html = render_report_html(build_report_context(db.get(Engagement, eid)))
    assert "ck-satisfied" in html and "ck-deficient" in html  # the fixture really produced both badges
    path = tmp_path / "attestation.html"
    path.write_text(html, encoding="utf-8")

    page = browser.new_page()
    try:
        page.goto(path.as_uri(), wait_until="load")
        page.emulate_media(media="print", color_scheme="dark")
        satisfied = _computed(page, ".ck-badge.ck-satisfied", "backgroundColor")
        assert satisfied == LIGHT_ACCENT_INK, (
            f"SATISFIED printed as white text on {satisfied}; the dark accent {DARK_ACCENT_INK} behind "
            "#fff is 1.6:1 — the word is illegible on the printed checklist"
        )
        assert _computed(page, ".ck-badge.ck-satisfied", "color") == "rgb(255, 255, 255)"
        assert _computed(page, ".ck-badge.ck-deficient", "backgroundColor") == LIGHT_HIGH
        # ...and the fill has to survive "Background graphics: off" or the badge prints as bare text.
        assert _computed(page, ".ck-badge.ck-satisfied", "printColorAdjust") == "exact"
    finally:
        page.close()


def test_screen_rendering_still_follows_the_viewers_colour_scheme(report_page):
    """The print pin must not leak into on-screen rendering: a dark-mode viewer still gets the dark ramp
    on screen."""
    report_page.emulate_media(media="screen", color_scheme="dark")
    assert _computed(report_page, ".sevbar .seg.sev-high", "backgroundColor") == DARK_HIGH


# ── the client-visible artifact: a PDF printed the way the dialog defaults ─────────────────────────


@pytest.mark.parametrize(("colour", "label"), [(PX_CRITICAL, "critical"), (PX_HIGH, "high")])
def test_pdf_printed_without_background_graphics_keeps_the_severity_fills(
    report_page, tmp_path, colour, label
):
    """The end-to-end version, in the exact configuration the client had: Chrome's
    ``print_background=False``, the print dialog's default.

    Both PDFs are printed and compared to each other, so the claim is relative and needs no magic
    pixel count: printing WITHOUT background graphics must not lose the severity colour that printing
    WITH them has. Measured on this fixture — with the defect: 504 painted pixels vs 6774 (the bar and
    every legend swatch gone); with the fix: 6773 vs 6774. (Both PDFs now carry the ext#43 cover page and
    contents, so the comparison is over every page — see ``_pdf_pages_rgb``.)
    """
    if shutil.which("pdftoppm") is None:  # pragma: no cover - environment-dependent
        pytest.skip("poppler's pdftoppm is not installed; skipping the rasterized PDF check (skip-clean)")
    nobg, withbg = tmp_path / "nobg.pdf", tmp_path / "withbg.pdf"
    report_page.pdf(path=str(nobg), format="Letter", print_background=False)
    report_page.pdf(path=str(withbg), format="Letter", print_background=True)

    painted_with = _count_colour(_pdf_pages_rgb(withbg), colour)
    painted_without = _count_colour(_pdf_pages_rgb(nobg), colour)
    assert painted_with > 2000, f"fixture problem: {label} is barely on the page even WITH backgrounds"
    assert painted_without >= painted_with * 0.9, (
        f"printing with background graphics off lost the {label} fill: {painted_without} painted pixels "
        f"vs {painted_with} with them on — this is what the client received"
    )


def test_ctrl_p_opens_the_child_findings_so_the_figure_sequence_starts_at_one(
    browser, session_factory, tmp_path
):
    """ext#117 — a nested child's evidence is numbered BEFORE the parent's own gallery, and children
    live in a ``<details class="children">`` that is CLOSED by default. The toolbar's Print button
    opened them; Ctrl+P / File -> Print / Save as PDF did not, so the primary client deliverable
    printed a PDF whose figure sequence began at "Figure 2" while the ``.docx`` began at "Figure 1".

    Driven through a real browser because that is the only place ``beforeprint`` exists: asserting on
    the emitted JS string would pass for a listener that is registered and wrong."""
    from scribble.models import Artifact, ArtifactKind, ArtifactPlacement

    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
        "53de0000000c4944415478da6360606000000005000166ff0f0e0000000049454e44ae426082"
    )
    store = tmp_path / "artifacts"
    store.mkdir(exist_ok=True)
    (store / "child.png").write_bytes(png)
    with session_factory() as db:
        client = Client(name="PrintChildren")
        db.add(client)
        db.flush()
        eng = Engagement(name="Print Children", client_id=client.id, company_name="PrintChildren")
        grp = FindingGroup(engagement=eng, name="Web", order_index=0)
        parent = EngagementFinding(
            engagement=eng, group=grp, title="Reflected XSS", severity=Severity.high, order_index=0,
            content_json={"description": _block("Reflected XSS on /search.")},
        )
        db.add(eng)
        db.flush()
        child = EngagementFinding(
            engagement=eng, group=grp, title="Reflected XSS host b", severity=Severity.high,
            order_index=1, parent_id=parent.id,
            content_json={"description": _block("Same issue, host b.")},
        )
        db.add(child)
        db.flush()
        db.add(Artifact(
            engagement_id=eng.id, finding_id=child.id, kind=ArtifactKind.screenshot,
            placement=ArtifactPlacement.attached, filename="child.png", caption="Host b payload",
            content_type="image/png", storage_path=str(store / "child.png"),
            order_index=0, byte_size=len(png),
        ))
        db.commit()
        eid = eng.id
    with session_factory() as db:
        html = render_report_html(
            build_report_context(db.get(Engagement, eid)),
            inline_assets=True,
            artifact_bytes=lambda path: Path(path).read_bytes(),
        )
    path = tmp_path / "report.html"
    path.write_text(html, encoding="utf-8")
    page = browser.new_page(viewport={"width": 1400, "height": 1000})
    try:
        page.goto(path.as_uri(), wait_until="load")
        assert page.locator("details.children").count() >= 1
        assert page.evaluate(
            "() => [...document.querySelectorAll('details.children')].every(d => !d.open)"
        ), "the fixture is wrong: children should start CLOSED, or this guard proves nothing"
        # Chromium dispatches beforeprint for real on the print path; dispatching it directly is the
        # same event the print engine sends and keeps the test off printToPDF's timing.
        page.evaluate("() => window.dispatchEvent(new Event('beforeprint'))")
        assert page.evaluate(
            "() => [...document.querySelectorAll('details.children')].every(d => d.open)"
        ), "Ctrl+P would print a PDF with the child figures missing from the sequence"
        # ...and the figure the child owns is Figure 1, i.e. the sequence a reader sees starts at 1.
        first = page.evaluate(
            "() => (document.querySelector('details.children figcaption')||{}).textContent || ''"
        )
        assert first.startswith("Figure 1 —"), first
    finally:
        page.close()


# ── #622: the Retest Closeout section (finding -> most-recent retest outcome) ──────────────────────────
#
# NON-browser structural cases: the closeout is a text table, so a real render + string/docx assertion is
# the right instrument (the pixel cases above are for colour on paper). The backward-compat guarantee is
# the same one every additive report block carries — an engagement with no retest renders identically to
# before this section existed — pinned here with a real red-then-green short-circuit test, not a green
# assertion that could pass for the wrong reason.


def _closeout_engagement(session_factory, *, with_retest: bool, outcome=None):
    """One engagement, one finding, optionally one recorded retest round on it. Returns the engagement id."""
    import uuid
    from datetime import date

    from scribble.enums import RetestOutcome
    from scribble.findings_service import record_retest

    with session_factory() as db:
        # Client.name is UNIQUE — keep it distinct, but with NO outcome/status token in it, or the raw
        # value would render in "Prepared for <client>" and defeat the label-vs-raw-value assertions below.
        client = Client(name=f"Closeout {uuid.uuid4()}")
        db.add(client)
        db.flush()
        eng = Engagement(name="Closeout Assessment", client_id=client.id, company_name="Acme Corp")
        grp = FindingGroup(engagement=eng, name="External", order_index=0)
        finding = EngagementFinding(
            engagement=eng, group=grp, title="Reflected XSS", severity=Severity.high, order_index=0,
            content_json={"description": _block("Reflected XSS on /search.")},
        )
        db.add(eng)
        db.flush()
        if with_retest:
            record_retest(
                db, finding, outcome or RetestOutcome.remediated,
                tested_by="alice", tested_on=date(2026, 9, 4),
            )
        db.commit()
        return eng.id


def _closeout_html(session_factory, eid: str) -> str:
    with session_factory() as db:
        return render_report_html(build_report_context(db.get(Engagement, eid)))


def test_no_retest_renders_no_closeout_section(session_factory):
    """Backward-compat: a report whose findings have no recorded retest has no closeout section, anchor,
    or table — byte-identical to before this block existed."""
    html = _closeout_html(session_factory, _closeout_engagement(session_factory, with_retest=False))
    assert "sec-retest" not in html
    assert "Retest Closeout" not in html
    assert 'class="rc-table"' not in html


def test_recorded_retest_renders_the_closeout_table(session_factory):
    from scribble.enums import RetestOutcome

    eid = _closeout_engagement(
        session_factory, with_retest=True, outcome=RetestOutcome.partially_remediated
    )
    html = _closeout_html(session_factory, eid)
    assert "sec-retest" in html
    assert "Retest Closeout" in html
    assert 'class="rc-table"' in html
    # the finding's most-recent outcome, client-facing label (not the raw enum value)
    assert "Partially remediated" in html
    assert "partially_remediated" not in html
    # the row links back to the finding's own anchor, which the findings block really emits
    with session_factory() as db:
        eng = db.get(Engagement, eid)
        fid = eng.findings[0].id
    assert f'href="#finding-{fid}"' in html
    assert f'id="finding-{fid}"' in html  # target exists -> no dangling anchor


def test_removing_the_empty_short_circuit_breaks_backward_compat(session_factory, monkeypatch):
    """The red half: neuter ``_render_retest_closeout``'s empty short-circuit and a no-retest report grows
    a bogus empty closeout section — proving the short-circuit is what keeps backward-compat, not luck."""
    from scribble.reporting import render_html as rh

    eid = _closeout_engagement(session_factory, with_retest=False)
    # A no-retest engagement: the real function short-circuits to "" -> no section.
    assert "sec-retest" not in _closeout_html(session_factory, eid)  # GREEN baseline

    # Drop the empty short-circuit: a version that emits the anchor unconditionally now leaks a section
    # into a report that has no retest at all.
    monkeypatch.setattr(
        rh, "_render_retest_closeout", lambda ctx: '<section id="sec-retest"></section>'
    )
    assert "sec-retest" in _closeout_html(session_factory, eid)  # RED: the guard is what suppressed it

    monkeypatch.undo()
    assert "sec-retest" not in _closeout_html(session_factory, eid)  # GREEN again once restored


def test_closeout_consumes_report_disposition_when_it_is_importable(session_factory, monkeypatch):
    """The ext#166 seam: inclusion is one helper call, so when ``report_disposition`` exists a finding it
    calls EXCLUDED drops out of the closeout even though it has a retest. Simulated by patching the (still
    unmerged) symbols onto ``scribble.enums`` — the single integration point imports them from there."""
    import scribble.enums as en

    monkeypatch.setattr(en, "DISPOSITION_EXCLUDED", "excluded", raising=False)
    monkeypatch.setattr(en, "report_disposition", lambda status: "excluded", raising=False)

    eid = _closeout_engagement(session_factory, with_retest=True)
    html = _closeout_html(session_factory, eid)
    assert "sec-retest" not in html  # disposition EXCLUDED -> not in the closeout

    monkeypatch.setattr(en, "report_disposition", lambda status: "live")
    html_live = _closeout_html(session_factory, eid)
    assert "sec-retest" in html_live  # disposition non-excluded -> back in


def test_docx_mirrors_the_closeout_table(session_factory):
    import io

    import docx
    from docx.oxml.ns import qn

    from scribble.enums import RetestOutcome
    from scribble.reporting.render_docx import render_report_docx

    eid = _closeout_engagement(session_factory, with_retest=True, outcome=RetestOutcome.not_remediated)
    with session_factory() as db:
        payload = render_report_docx(build_report_context(db.get(Engagement, eid)))
    doc = docx.Document(io.BytesIO(payload))
    text = "".join(t.text or "" for t in doc.element.body.iter(qn("w:t")))
    assert "Retest Closeout" in text
    assert "Reflected XSS" in text
    assert "Not remediated" in text


def test_docx_without_a_retest_has_no_closeout_heading(session_factory):
    import io

    import docx
    from docx.oxml.ns import qn

    from scribble.reporting.render_docx import render_report_docx

    eid = _closeout_engagement(session_factory, with_retest=False)
    with session_factory() as db:
        payload = render_report_docx(build_report_context(db.get(Engagement, eid)))
    doc = docx.Document(io.BytesIO(payload))
    text = "".join(t.text or "" for t in doc.element.body.iter(qn("w:t")))
    assert "Retest Closeout" not in text
