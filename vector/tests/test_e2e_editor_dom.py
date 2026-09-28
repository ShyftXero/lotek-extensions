"""Browser e2e: the editor's panels show a hostile model as text and never parse any of it as markup.

The editor page's ``#ved-model`` island is normalized server-side today (``edit_diagram`` runs
``normalize``), but the panels must not depend on that: the same runtime is handed whatever the island
holds. So the page here serves an UN-normalized model with markup, attribute breakouts and an
``onerror`` payload in every string field AND in the numeric fields the panel summaries print (edge
``at``, phase ``n``), then opens every tab, writable and read-only. Nothing may run, no element or
attribute may appear that the editor did not build, and every value must come back verbatim.

Also covers the post-save redirect: the new diagram's id comes from the server response, and the page
must navigate to ``<base>/edit/<id>`` with the id encoded, whatever it holds.

Skips cleanly when Playwright or a browser binary is unavailable (same posture as test_e2e_editor.py).
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from urllib.parse import unquote

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

from vector.render import json_for_script  # noqa: E402

STATIC = Path(__file__).resolve().parent.parent / "vector" / "static"

# Runs if any model string reaches an HTML parser: an element with an error handler, an attribute
# breakout in both quote styles, and an SVG element with a load handler.
X = '"\'><img src=x onerror="window.__xss=(window.__xss||0)+1"><svg onload="window.__xss=1"></svg>'
TABS = ("meta", "zones", "nodes", "edges", "phases", "style")

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>ved</title><meta name="csrf-token" content="t"></head>
<body>
<input id="ved-name" value="n"><button id="ved-save">Save</button>
<button id="ved-export-json">j</button><button id="ved-export-html">h</button>
<div class="ved" data-diagram-id="new" data-api-base="/vector/api" data-can-write="{can_write}"
     data-dashboard="/vector/" data-export-html-base="/vector/api/export/html">
  <div class="ved-tabs">{tabs}</div>
  <div class="ved-panel" data-panel></div>
  <label>Phase <span data-phase-num>0</span></label>
  <input type="range" data-scrub min="0" max="0" value="0"><span data-preview-note></span>
  <div class="ved-preview" data-preview></div>
</div>
<script type="application/json" id="ved-model">{model}</script>
<script src="/static/vector-viewer.js"></script>
<script src="/static/vector-editor.js"></script>
</body></html>"""


def _hostile_model() -> dict:
    """UN-normalized on purpose. Every string field carries X, and so do the two numbers the panel
    summaries print (``edges[].at``, ``phases[].n``) and the values that become <option>s."""
    return {
        "meta": {"title": X, "subtitle": X, "badge": X, "railLabels": [X, X],
                 "intro": {"eyebrow": X, "objective": X, "readingNotes": X, "note": X}},
        "zones": [{"id": X, "title": X, "subtitle": X, "accent": X, "order": X},
                  {"id": "z2", "title": "", "subtitle": ""}],
        "nodes": [
            {"id": X, "label": X, "ip": X, "domain": X, "zone": X, "row": X, "role": X, "dualIp": X,
             "context": True, "activateAt": X, "states": [{"at": X, "state": X, "label": X}],
             "reIp": {"at": X, "ip": X, "domain": X}},
            {"id": "b", "label": "", "zone": "nowhere"},
        ],
        "edges": [{"id": X, "from": X, "to": X, "kind": X, "at": X, "route": X, "offset": X, "label": X}],
        "phases": [
            {"n": 0, "intro": True},
            {"n": X, "title": X, "mitre": X, "desc": X, "watch": X, "note": X, "targets": [X],
             "tactics": [{"label": X, "kind": X}],
             "blue": {"tool": X, "finding": X, "query": X, "seen": X, "note": X, "gap": True}},
        ],
        "style": {"edgeKinds": {X: {"color": X}}},
    }


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def server():
    from flask import Flask, Response, abort, jsonify
    from werkzeug.serving import make_server

    app = Flask(__name__)
    state: dict = {"new_id": "x"}
    tabs = "".join(f'<button class="ved-tab" data-tab="{t}">{t}</button>' for t in TABS)

    @app.get("/static/<name>")
    def asset(name):
        if name not in ("vector-viewer.js", "vector-editor.js"):
            abort(404)
        return Response((STATIC / name).read_text(encoding="utf-8"), mimetype="text/javascript")

    @app.get("/vector/new")
    def new_page():
        return Response(_PAGE.format(can_write="1", tabs=tabs, model=json_for_script(_hostile_model())),
                        mimetype="text/html")

    @app.get("/vector/view")
    def ro_page():
        return Response(_PAGE.format(can_write="0", tabs=tabs, model=json_for_script(_hostile_model())),
                        mimetype="text/html")

    @app.post("/vector/api/diagrams")
    def create():
        return jsonify({"id": state["new_id"]})

    @app.get("/vector/edit/<path:rest>")
    def edited(rest):
        return Response("<!doctype html><title>edited</title>", mimetype="text/html")

    port = _free_port()
    srv = make_server("127.0.0.1", port, app)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)
    yield f"http://127.0.0.1:{port}", state
    srv.shutdown()


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


# What the panel holds: any handler attribute or foreign element, every data-bind path, and the text
# and values the operator sees.
_INSPECT = """() => {
  var panel = document.querySelector('[data-panel]');
  var bad = [], binds = [], values = [], tags = {};
  panel.querySelectorAll('*').forEach(function (el) {
    tags[el.tagName.toLowerCase()] = 1;
    for (var i = 0; i < el.attributes.length; i++) {
      if (/^on/i.test(el.attributes[i].name)) bad.push(el.tagName + ' ' + el.attributes[i].name);
    }
    if (el.hasAttribute('data-bind')) binds.push(el.getAttribute('data-bind'));
    if (el.matches('input[type=text], textarea')) values.push(el.value);
    if (el.matches('option')) values.push(el.value);
  });
  var summaries = Array.from(panel.querySelectorAll('summary')).map(function (s) { return s.textContent; });
  return { bad: bad, binds: binds, values: values, summaries: summaries, tags: Object.keys(tags).sort(),
           xss: window.__xss || null };
}"""

PANEL_TAGS = {"b", "button", "details", "div", "input", "label", "option", "p", "select", "span", "summary",
              "textarea"}


@pytest.mark.parametrize("path", ["/vector/new", "/vector/view"], ids=["writable", "read-only"])
def test_every_tab_shows_a_hostile_model_as_text(server, browser, path):
    base, _ = server
    page = browser.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(base + path)
    page.wait_for_selector("[data-panel] .ved-field")
    for tab in TABS:
        page.click(f'.ved-tab[data-tab="{tab}"]')
        for d in page.query_selector_all("[data-panel] details"):
            d.evaluate("el => { el.open = true; }")
        page.wait_for_timeout(30)
        got = page.evaluate(_INSPECT)
        assert got["xss"] is None, f"{tab}: a model string ran"
        assert got["bad"] == [], f"{tab}: {got['bad']}"
        assert set(got["tags"]) <= PANEL_TAGS, f"{tab}: {got['tags']}"
        for b in got["binds"]:
            assert all(c.isalnum() or c in "._" for c in b), f"{tab}: data-bind {b!r}"
        if tab != "style":
            assert X in got["values"], f"{tab}: the hostile value is not shown verbatim"
        if tab == "style":
            shown = json.loads(page.locator("[data-panel] textarea.ved-json").input_value())
            assert shown == _hostile_model()["style"], "the style JSON is not shown verbatim"
    page.click('.ved-tab[data-tab="phases"]')
    summaries = page.evaluate(_INSPECT)["summaries"]  # phases tab: the unescaped phase number
    assert any(("phase " + X) in s for s in summaries), summaries
    page.click('.ved-tab[data-tab="edges"]')
    summaries = page.evaluate(_INSPECT)["summaries"]  # edges tab: the unescaped edge `at`
    assert any((" @" + X) in s for s in summaries), summaries
    assert page.evaluate("() => window.__xss || null") is None
    assert errors == [], errors
    page.close()


@pytest.mark.parametrize("new_id", ["0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b", "javascript:window.__xss=1",
                                    X, "../../evil"])
def test_save_redirect_stays_on_the_edit_route(server, browser, new_id):
    base, state = server
    state["new_id"] = new_id
    page = browser.new_page()
    page.goto(base + "/vector/new")
    page.wait_for_selector("[data-panel] .ved-field")
    with page.expect_navigation():
        page.click("#ved-save")
    url = page.url
    assert url.startswith(base + "/vector/edit/"), url
    assert unquote(url[len(base + "/vector/edit/"):]) == new_id
    page.close()


def test_hostile_model_is_really_hostile():
    """Guard against a vacuous pass: the served island still carries the payload, un-normalized."""
    island = json_for_script(_hostile_model())
    assert json.loads(island)["edges"][0]["at"] == X
