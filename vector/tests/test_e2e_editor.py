"""Browser e2e: author a diagram in the editor and confirm the preview + exported HTML render.

Runs a real standalone server + Chromium via Playwright. Skips cleanly when Playwright or a browser
binary is unavailable (so a plain ``pytest`` on a bare box no-ops it instead of failing).
"""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import time

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def server():
    from werkzeug.serving import make_server

    from vector.standalone import create_app

    d = tempfile.mkdtemp()
    app = create_app(db_path=os.path.join(d, "e2e.sqlite"), instance_path=d, testing=True, seed=True)
    port = _free_port()
    srv = make_server("127.0.0.1", port, app)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.3)
    yield f"http://127.0.0.1:{port}"
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


def test_example_renders_and_advances(server, browser):
    page = browser.new_page()
    page.goto(f"{server}/vector/")
    # open the seeded example
    page.click("table.vec-tbl tbody tr:first-child a")
    page.wait_for_selector(".ved-preview svg.map g.node", state="attached")
    assert page.locator(".ved-preview svg.map g.node").count() > 0
    # advance a phase via the preview's Next button; an edge should draw
    page.click(".ved-preview [data-next]")
    page.wait_for_selector(".ved-preview svg.map path.edge", state="attached")
    assert page.locator(".ved-preview svg.map path.edge").count() > 0


def test_author_new_diagram_and_export(server, browser, tmp_path):
    page = browser.new_page()
    page.goto(f"{server}/vector/new")
    page.wait_for_selector(".ved")
    # add a zone, a node, and a phase via the editor
    page.click('.ved-tab[data-tab="zones"]')
    page.click('[data-action="add-zone"]')
    page.click('.ved-tab[data-tab="nodes"]')
    page.click('[data-action="add-node"]')
    page.click('.ved-tab[data-tab="phases"]')
    page.click('[data-action="add-phase"]')
    # preview should now show the added node (wait for it, don't race the debounced refresh)
    page.wait_for_selector(".ved-preview svg.map g.node", state="attached")
    assert page.locator(".ved-preview svg.map g.node").count() > 0

    # export HTML -> a download that is a self-contained document
    with page.expect_download() as dl:
        page.click("#ved-export-html")
    path = dl.value.path()
    html = open(path, encoding="utf-8").read()
    assert "__VECTOR_MODEL__" in html and "VectorViewer" in html
    assert "<link" not in html


def test_malicious_style_cannot_inject(browser, tmp_path):
    """A crafted model.style accent must not break out of the inline style attribute (XSS guard)."""
    from vector.render import render_deliverable

    payload = '#000"><img src=x onerror="window.__pwned=1">'
    model = {
        "meta": {"title": "x"},
        "zones": [{"id": "z", "title": "Z", "accent": "red"}],
        "nodes": [{"id": "a", "zone": "z", "label": "n", "states": [{"at": 0, "state": "owned"}]}],
        "edges": [], "phases": [{"n": 1, "title": "p", "targets": ["a"]}],
        "style": {"nodeStates": {"owned": {"accent": payload, "precedence": 3, "fillNode": True}}},
    }
    out = tmp_path / "evil.html"
    out.write_text(render_deliverable(model), encoding="utf-8")
    page = browser.new_page()
    page.goto(out.as_uri())
    page.wait_for_selector("svg.map g.node", state="attached")
    assert page.evaluate("() => window.__pwned") in (None, False)
    assert page.locator("svg.map img").count() == 0  # no injected element survived
