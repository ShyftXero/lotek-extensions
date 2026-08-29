"""ext#115 — on PAPER the deliverable shows the FINAL keyframe, not whatever phase it was on.

The exported deliverable is an animated walkthrough. Printed (on its own, or embedded in scribble's
report iframe) it rasterizes at whichever phase it happens to be showing — normally phase 0, the intro,
where no edge has been drawn yet. So a printed attack path was an empty diagram: "an animated iframe is
equally meaningless on paper" (ext#115).

These run through Chromium's REAL print path (``page.pdf()`` drives ``Page.printToPDF``, which
dispatches ``beforeprint``/``afterprint``) rather than dispatching the events by hand, because the whole
question is whether the browser's print actually reaches this code — a hand-dispatched event would
prove only that the listener body works, which was never in doubt.

SKIP-CLEAN: no Playwright / no Chromium binary -> skip, never fail.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

from vector.render import render_deliverable  # noqa: E402

# Three phases and two edges that only exist at the LAST one, so "printed at the final phase" and
# "printed at the phase it booted on" are distinguishable by counting drawn edges (0 vs 2).
MODEL = {
    "meta": {"title": "Print check"},
    "zones": [{"id": "ext", "title": "Internet"}, {"id": "core", "title": "Core"}],
    "nodes": [
        {"id": "a", "zone": "ext", "label": "Operator"},
        {"id": "b", "zone": "core", "label": "DC01"},
        {"id": "c", "zone": "core", "label": "FS01", "row": 1},
    ],
    "edges": [
        {"id": "e1", "from": "a", "to": "b", "at": 1, "kind": "attack"},
        {"id": "e2", "from": "b", "to": "c", "at": 3, "kind": "attack"},
    ],
    "phases": [{"n": 0, "intro": True}, {"n": 1, "title": "one"}, {"n": 3, "title": "three"}],
}

_EDGES = "svg.map path.edge"


@pytest.fixture(scope="module")
def browser():
    try:
        with sync_playwright() as p:
            try:
                b = p.chromium.launch()
            except Exception as exc:  # noqa: BLE001 - no browser binary installed
                pytest.skip(f"chromium unavailable: {exc}")
            yield b
            b.close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"playwright unavailable: {exc}")


@pytest.fixture
def page(browser, tmp_path):
    out = tmp_path / "deliverable.html"
    out.write_text(render_deliverable(MODEL), encoding="utf-8")
    page = browser.new_page()
    page.goto(out.as_uri())
    page.wait_for_selector("svg.map g.node", state="attached")
    yield page
    page.close()


def test_printing_advances_to_the_final_keyframe(page, tmp_path):
    """The defect and its fix, measured DURING the print. A listener registered after the viewer's own
    sees the state the print engine will rasterize."""
    assert page.locator(_EDGES).count() == 0, "expected the walkthrough to boot on the intro phase"
    page.evaluate(
        "() => { window.__printedEdges = null;"
        " window.addEventListener('beforeprint', () => {"
        "   window.__printedEdges = document.querySelectorAll('svg.map path.edge').length; }); }"
    )
    page.pdf(path=str(tmp_path / "out.pdf"))
    assert page.evaluate("() => window.__printedEdges") == 2


def test_printing_restores_the_phase_the_reader_was_on(page, tmp_path):
    """Print is not supposed to be a destructive act on the open document.

    The restore is only meaningful if something MOVED, so the jump is measured mid-print as well —
    without that, deleting both listeners left the phase untouched and this test still passed."""
    page.click("[data-next]")  # reader steps to phase 1
    assert page.locator(_EDGES).count() == 1
    page.evaluate(
        "() => { window.__mid = null;"
        " window.addEventListener('beforeprint', () => {"
        "   window.__mid = document.querySelectorAll('svg.map path.edge').length; }); }"
    )
    page.pdf(path=str(tmp_path / "out.pdf"))
    assert page.evaluate("() => window.__mid") == 2, "the print did not advance to the final keyframe"
    assert page.locator(_EDGES).count() == 1


def test_a_second_print_does_not_strand_the_reader_at_the_end(page, tmp_path):
    """``resume`` was assigned unconditionally, so a second ``beforeprint`` before any ``afterprint``
    overwrote the reader's phase with the LAST one — and ``afterprint`` then "restored" them to the end
    of the walkthrough, permanently. Chrome fires one ``beforeprint`` per ``window.print()``."""
    page.click("[data-next]")  # reader is on phase 1
    assert page.locator(_EDGES).count() == 1
    page.evaluate("() => { window.dispatchEvent(new Event('beforeprint'));"
                  "        window.dispatchEvent(new Event('beforeprint'));"
                  "        window.dispatchEvent(new Event('afterprint')); }")
    assert page.locator(_EDGES).count() == 1, "the reader was stranded on the final keyframe"


def test_print_media_hides_the_walkthrough_controls(page):
    """Step buttons and a phase rail are interactive controls; on paper they are furniture with
    nothing to click, and they crowd out the topology the page is actually for."""
    page.emulate_media(media="print")
    assert page.locator(".vap .controls").is_hidden()
    assert page.locator(".vap .rail").is_hidden()
    assert page.locator("svg.map").is_visible()


def test_print_media_stops_the_animations(page):
    """A settled frame rasterizes; a mid-keyframe one smears. Measured as a computed style, not as a
    string in the stylesheet."""
    page.evaluate("() => window.dispatchEvent(new Event('beforeprint'))")
    page.emulate_media(media="print")
    page.wait_for_selector(_EDGES, state="attached")
    names = page.eval_on_selector_all(
        _EDGES, "els => els.map(e => getComputedStyle(e).animationName)"
    )
    assert names, "expected drawn edges to measure"
    assert all(n in ("none", "") for n in names), names
