"""Browser e2e: the viewer under a strict CSP, tour mode, per-step highlight, step media, deep links.

The page is served with ``Content-Security-Policy: script-src 'self'; style-src 'self'`` and carries no
inline script or style: the runtime + sheet load from files and the model rides in a
``<script type="application/json" id="vap-model">`` island that ``boot()`` reads itself. Any inline
style attribute, eval or inline script would be refused by the browser and reported here.

Skips cleanly when Playwright or a browser binary is unavailable (same posture as test_e2e_editor.py).
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

from vector.render import json_for_script  # noqa: E402
from vector.schema import normalize  # noqa: E402
from vector.seed import TOUR_FILE, _model, load_example  # noqa: E402

STATIC = Path(__file__).resolve().parent.parent / "vector" / "static"
CSP = "script-src 'self'; style-src 'self'"

# Catch violations two ways: the securitypolicyviolation event (registered by an init script, which the
# browser runs outside the page's CSP) and the console message Chromium logs for every refusal.
_CSP_LISTENER = """
window.__csp = [];
document.addEventListener('securitypolicyviolation', function (e) {
  window.__csp.push(e.violatedDirective + ' ' + (e.blockedURI || '') + ' ' + (e.sample || ''));
});
"""

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>vector csp</title>
<link rel="stylesheet" href="/vector-viewer.css"></head>
<body><div id="vap"></div>
<script type="application/json" id="vap-model">{model}</script>
<script src="/vector-viewer.js"></script>
</body></html>"""


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def server():
    """A bare CSP-enforcing server: /<name>/ serves a page for one of MODELS, plus the two assets."""
    from flask import Flask, Response, abort
    from werkzeug.serving import make_server

    app = Flask(__name__)

    @app.after_request
    def _csp(resp):
        resp.headers["Content-Security-Policy"] = CSP
        return resp

    @app.get("/vector-viewer.<ext>")
    def asset(ext):
        if ext not in ("js", "css"):
            abort(404)
        mime = "text/javascript" if ext == "js" else "text/css"
        return Response((STATIC / f"vector-viewer.{ext}").read_text(encoding="utf-8"), mimetype=mime)

    @app.get("/<name>/")
    def page(name):
        if name not in MODELS:
            abort(404)
        return Response(_PAGE.format(model=json_for_script(MODELS[name]())), mimetype="text/html")

    port = _free_port()
    srv = make_server("127.0.0.1", port, app)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def _media_model() -> dict:
    """UN-normalized on purpose: the editor previews raw models, so the runtime must filter by itself."""
    return {
        "meta": {"title": "Media", "mode": "tour"},
        "zones": [{"id": "z", "title": "Z", "accent": "cyan"}],
        "nodes": [{"id": "a", "zone": "z", "label": "A"}],
        "edges": [],
        "phases": [
            {"n": 0, "intro": True},
            {"n": 1, "title": "hostile", "targets": ["a"], "image": "https://evil.example/x.png",
             "links": [{"label": "<b>docs</b>", "href": "/docs#dataflow-3"},
                       {"label": "evil", "href": "http://evil.example"},
                       {"label": "js", "href": "javascript:window.__pwned=1"},
                       {"label": "js2", "href": "java\tscript:window.__pwned=1"},
                       {"label": "proto", "href": "//evil.example/docs"},
                       {"label": "off-docs", "href": "/admin"}]},
            {"n": 2, "title": "fine", "image": "data:image/gif;base64,R0lGODlhAQABAAAAACw=",
             "links": [{"label": "rel", "href": "guide.html"}]},
        ],
    }


def _bad_rail_model() -> dict:
    """UN-normalized: a raw editor model whose railLabels is not a list must still mount."""
    return {"meta": {"title": "Bad rail", "railLabels": "not-a-list"},
            "zones": [{"id": "z", "title": "Z"}], "nodes": [{"id": "a", "zone": "z", "label": "A"}],
            "edges": [], "phases": [{"n": 1, "title": "one", "targets": ["a"]}]}


MODELS = {
    "badrail": _bad_rail_model,
    "tour": lambda: normalize(load_example(TOUR_FILE)),
    "attack": lambda: normalize(_model()),
    "media": _media_model,
}


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
def open_page(browser):
    pages = []

    def _open(url):
        page = browser.new_page()
        page.console_csp = []
        page.on("console", lambda m: (
            page.console_csp.append(m.text) if "Content Security Policy" in m.text else None))
        page.add_init_script(_CSP_LISTENER)
        page.goto(url)
        page.wait_for_selector("#vap svg.map g.node", state="attached")
        pages.append(page)
        return page

    yield _open
    for p in pages:
        p.close()


def _violations(page) -> list[str]:
    return list(page.evaluate("() => window.__csp")) + page.console_csp


def _eyebrow(page) -> str:
    return page.text_content("#vap .brief .eyebrow")  # raw text; the sheet uppercases it on screen


_TEXTS = "els => els.map(e => e.textContent)"
_HREFS = "els => els.map(e => e.getAttribute('href'))"


def _focused(page) -> set[str]:
    """Labels of the nodes highlighted on the map right now."""
    return set(page.eval_on_selector_all("#vap svg.map g.node.is-focus text.nm", _TEXTS))


TOUR = normalize(load_example(TOUR_FILE))
LABEL = {n["id"]: n["label"] for n in TOUR["nodes"]}
STEPS = [p for p in TOUR["phases"] if not p.get("intro")]


def test_tour_mounts_and_steps_under_strict_csp(server, open_page):
    page = open_page(f"{server}/tour/")
    for _ in STEPS:
        page.click("#vap [data-next]")
    page.click("#vap [data-reset]")
    assert _violations(page) == []


def test_attack_path_mounts_and_steps_under_strict_csp(server, open_page):
    page = open_page(f"{server}/attack/")
    for _ in range(8):
        page.click("#vap [data-next]")
    page.click('#vap .detail-tab[data-tab="blue"]')
    assert _violations(page) == []
    # the per-kind colours still arrive — via the CSSOM, not an attribute
    styled = page.eval_on_selector_all("#vap svg.map path.edge",
                                       "els => els.filter(e => e.style.stroke).length")
    assert styled > 0
    assert page.evaluate("() => document.querySelectorAll('#vap [data-vs]').length") == 0


def test_tour_mode_is_neutral(server, open_page):
    page = open_page(f"{server}/tour/")
    assert page.text_content("#vap [data-brand]") == "How lotek works"
    assert "◤" not in page.inner_text("#vap header")
    assert page.get_attribute("#vap svg.map", "aria-label") == "Walkthrough map"
    page.click("#vap [data-next]")
    assert _eyebrow(page) == "Step 01 / 9"
    brief = page.text_content("#vap .brief-scroll")
    assert page.locator("#vap .detail-tab").count() == 0
    for word in ("Red Team", "Blue Team", "Targets this phase", "attack path", "Phase"):
        assert word.lower() not in brief.lower(), word
    assert "on the map" in brief.lower()


def test_attack_path_mode_is_unchanged(server, open_page):
    page = open_page(f"{server}/attack/")
    assert "◤" in page.inner_text("#vap [data-brand]")
    assert page.get_attribute("#vap svg.map", "aria-label") == "Attack path topology"
    page.click("#vap [data-next]")
    assert page.locator("#vap .detail-tab").count() == 2
    assert "Targets this phase" in page.text_content("#vap .brief-scroll")
    assert page.locator("#vap svg.map .focus-ring").count() == 0


def test_each_step_highlights_only_its_own_targets(server, open_page):
    page = open_page(f"{server}/tour/")
    assert _focused(page) == set()  # the intro highlights nothing
    for step in STEPS:
        page.click("#vap [data-next]")
        assert _focused(page) == {LABEL[t] for t in step["targets"]}, step["title"]
        assert page.locator("#vap svg.map .focus-ring").count() == len(step["targets"])
    page.click("#vap [data-prev]")  # going back re-derives, it doesn't accumulate
    assert _focused(page) == {LABEL[t] for t in STEPS[-2]["targets"]}


def test_step_links_and_image_are_sanitised_by_the_runtime(server, open_page):
    page = open_page(f"{server}/media/")
    page.click("#vap [data-next]")
    links = page.eval_on_selector_all("#vap .step-links a",
                                      "els => els.map(e => [e.textContent, e.getAttribute('href')])")
    assert links == [["<b>docs</b>", "/docs#dataflow-3"]]  # label as text, hostile targets gone
    assert page.locator("#vap .step-links b").count() == 0
    assert page.locator("#vap .step-img").count() == 0  # https image refused
    page.click("#vap [data-next]")
    assert page.get_attribute("#vap .step-img", "src").startswith("data:image/gif;base64,")
    assert page.eval_on_selector_all("#vap .step-links a", _HREFS) == ["guide.html"]
    assert page.evaluate("() => window.__pwned") in (None, False)
    assert _violations(page) == []


def test_tour_steps_render_their_docs_links(server, open_page):
    page = open_page(f"{server}/tour/")
    page.click("#vap [data-next]")
    hrefs = page.eval_on_selector_all("#vap .step-links a", _HREFS)
    assert hrefs == [link["href"] for link in STEPS[0]["links"]]


def test_deep_link_selects_the_step_and_follows_it_without_history_spam(server, open_page):
    page = open_page(f"{server}/tour/#step-3")
    assert "03" in _eyebrow(page)
    assert _focused(page) == {LABEL[t] for t in STEPS[2]["targets"]}
    length = page.evaluate("() => history.length")
    page.click("#vap [data-next]")
    page.click("#vap [data-next]")
    assert page.evaluate("() => location.hash") == "#step-5"
    assert page.evaluate("() => history.length") == length  # replaceState, not a new entry per step
    page.evaluate("() => { location.hash = '#step-1'; }")
    page.wait_for_function("() => document.querySelector('#vap .brief .eyebrow').textContent.includes('01')")


def test_no_hash_is_written_until_the_reader_moves(server, open_page):
    page = open_page(f"{server}/tour/")
    assert page.evaluate("() => location.hash") == ""



@pytest.mark.parametrize("name", ["tour", "attack"])
def test_the_progress_rail_renders_and_drives_the_viewer_in_both_modes(server, open_page, name):
    """The rail was dead markup: ``buildRail()`` existed but nothing called it, so ``[data-rail]`` stayed
    empty in every mode. One segment per step, painted as the reader moves, and a click jumps."""
    page = open_page(f"{server}/{name}/")
    segs = "#vap [data-rail] .seg"
    word = "Step" if name == "tour" else "Phase"
    assert page.locator(f"{segs}.cur").count() == 0  # the intro is step 0, before the first segment
    labels = page.eval_on_selector_all("#vap [data-rail-labels] span", _TEXTS)
    assert labels == list(MODELS[name]()["meta"].get("railLabels") or [])

    page.click("#vap [data-next]")
    total = int(_eyebrow(page).rsplit("/", 1)[1])  # "Step 01 / 9" -> the viewer's own step count
    assert total > 1
    assert page.eval_on_selector_all(segs, "els => els.map(e => e.title)") == [
        f"{word} {i}" for i in range(1, total + 1)]
    page.click("#vap [data-next]")
    assert page.eval_on_selector_all(segs, "els => els.map(e => e.className)")[:3] == [
        "seg done", "seg cur", "seg"]

    page.click(f"{segs}:nth-child({total})")  # the rail is a jump control, not just a readout
    assert page.locator(f"{segs}.cur").count() == 1
    assert page.locator(f"{segs}.done").count() == total - 1
    assert page.get_attribute("#vap [data-next]", "disabled") is not None
    assert _violations(page) == []


def test_a_non_list_rail_label_field_does_not_break_the_mount(server, open_page):
    page = open_page(f"{server}/badrail/")
    assert page.locator("#vap [data-rail] .seg").count() == 1
    assert page.eval_on_selector_all("#vap [data-rail-labels] span", _TEXTS) == []
    assert _violations(page) == []
