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

import shutil
import subprocess
from pathlib import Path

import pytest

from scribble.content import schema
from scribble.enums import Severity
from scribble.models import Client, Engagement, EngagementFinding, FindingGroup
from scribble.reporting import build_report_context
from scribble.reporting.render_html import render_report_html

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
    unpainted white paper, for the whole document rather than one widget."""
    report_page.emulate_media(media="print", color_scheme="dark")
    ink = _computed(report_page, "body", "color")
    assert ink == LIGHT_INK, f"printed the dark ink {ink} (paper ink is {LIGHT_INK}) onto white paper"
    assert _computed(report_page, "body", "backgroundColor") == "rgb(255, 255, 255)"


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
