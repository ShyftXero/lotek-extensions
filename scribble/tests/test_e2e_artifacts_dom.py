"""Browser e2e: the evidence gallery builds its rows from nodes and never runs an artifact's strings.

``artifacts.js`` builds an optimistic row for each upload from the picked file's name, the typed caption
and a local ``blob:`` preview URL, then fills in the server's response (``data.id``, ``data.url``) when
the upload lands, or the server's error text when it fails. Each of those is fed markup, attribute
breakouts and script URLs here. The page must not run any of it or grow an element the gallery did not
build, and it must show the name, caption and error verbatim.

Loads the file into a blank page with a stub resilience outbox, so no server is needed. Skips cleanly
when Playwright or a browser binary is unavailable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

ARTIFACTS_JS = (Path(__file__).resolve().parents[1] / "scribble" / "static" / "artifacts.js").read_text(
    encoding="utf-8"
)

X = '"\'><img src=x onerror="window.__xss=(window.__xss||0)+1"><svg onload="window.__xss=1"></svg>'
BAD_URLS = ["javascript:window.__xss=1", " JaVaScRiPt:window.__xss=1", "java\tscript:window.__xss=1",
            "data:text/html,<script>parent.__xss=1</script>", '"><img src=x onerror=window.__xss=1>']

_PAGE = """<!doctype html><html><body>
<div class="scribble-gallery card" data-finding-id="f1" data-list-url="/scribble/api/findings/f1/artifacts"
     data-reorder-url="/scribble/api/findings/f1/artifacts/reorder" data-create-url="/scribble/api/artifacts">
  <form class="scribble-gallery-upload" data-engagement-id="e1">
    <input type="file" name="file" class="scribble-gallery-file" required />
    <input type="text" name="caption" class="scribble-gallery-caption-input" />
    <button type="submit" class="btn btn-primary">Attach</button>
  </form>
  <ul class="scribble-gallery-list"></ul>
  <p class="scribble-gallery-empty">none</p>
</div></body></html>"""

_OUTBOX_STUB = """
window.__handlers = {}; window.__uploads = [];
window.LotekReportingOutbox = {
  on: function (ev, fn) { window.__handlers[ev] = fn; },
  enqueueUpload: function (op) { window.__uploads.push(op); },
  setExternalPending: function () {},
};
"""

# Every row: handler attributes, tags, URLs, and what the operator sees.
_INSPECT = """() => {
  var rows = Array.from(document.querySelectorAll('.scribble-gallery-list > li'));
  var bad = [], tags = {}, hrefs = [], srcs = [];
  rows.forEach(function (li) {
    li.querySelectorAll('*').forEach(function (el) {
      tags[el.tagName.toLowerCase()] = 1;
      for (var i = 0; i < el.attributes.length; i++) {
        if (/^on/i.test(el.attributes[i].name)) bad.push(el.tagName + ' ' + el.attributes[i].name);
      }
      if (el.hasAttribute('href')) hrefs.push(el.getAttribute('href'));
      if (el.hasAttribute('src')) srcs.push(el.getAttribute('src'));
    });
  });
  return {
    bad: bad, tags: Object.keys(tags).sort(), hrefs: hrefs, srcs: srcs,
    names: rows.map(function (li) { return li.querySelector('.scribble-gallery-filename').textContent; }),
    captions: rows.map(function (li) { return li.querySelector('.scribble-gallery-caption').value; }),
    status: rows.map(function (li) {
      var s = li.querySelector('.scribble-gallery-pending-status'); return s ? s.textContent : null; }),
    xss: window.__xss || null,
  };
}"""

ROW_TAGS = {"a", "button", "div", "img", "input", "label", "span"}
PNG = bytes([137, 80, 78, 71, 13, 10, 26, 10])


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
def page(browser):
    pg = browser.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.set_content(_PAGE)
    pg.evaluate(_OUTBOX_STUB)
    pg.add_script_tag(content=ARTIFACTS_JS)
    yield pg
    pg.close()
    assert errors == [], errors


def _upload(page, name, mime="image/png", caption=X):
    page.set_input_files(".scribble-gallery-file", files=[{"name": name, "mimeType": mime, "buffer": PNG}])
    page.fill(".scribble-gallery-caption-input", caption)
    page.click(".scribble-gallery-upload button[type=submit]")
    return page.evaluate("() => window.__uploads[window.__uploads.length - 1].tempId")


def _assert_inert(page):
    got = page.evaluate(_INSPECT)
    assert got["xss"] is None
    assert got["bad"] == [], got["bad"]
    assert set(got["tags"]) <= ROW_TAGS, got["tags"]
    for url in got["hrefs"] + got["srcs"]:
        assert "script:" not in url.lower() and not url.lower().startswith("data:"), url
    return got


def test_pending_rows_show_hostile_names_and_captions_as_text(page):
    _upload(page, X + ".png")
    _upload(page, X + ".txt", mime="text/plain")
    got = _assert_inert(page)
    assert got["names"] == [X + ".png", X + ".txt"]
    assert got["captions"] == [X, X]
    assert all(s.startswith("blob:") for s in got["srcs"]), got["srcs"]


def test_resolved_upload_urls_and_ids_are_not_trusted(page):
    for url in BAD_URLS:
        temp = _upload(page, "shot.png")
        page.evaluate("(a) => window.__handlers.resolved(a[0], { id: a[1], url: a[2] })", [temp, X, url])
    got = _assert_inert(page)
    # A scripted URL is dropped (href "#", src ""); the scheme-less breakout is a relative URL, so it is
    # kept, but encoded: no quote or bracket survives into the attribute.
    assert got["hrefs"][:4] == ["#"] * 4 and got["srcs"][:4] == [""] * 4
    assert got["hrefs"][4] == got["srcs"][4] == "%22%3E%3Cimg%20src=x%20onerror=window.__xss=1%3E"
    for el in page.query_selector_all(".scribble-gallery-list a, .scribble-gallery-list img"):
        el.click(force=True, modifiers=[])
    page.wait_for_timeout(50)
    assert page.evaluate("() => window.__xss || null") is None


def test_resolved_upload_keeps_a_real_artifact_url(page):
    temp = _upload(page, "shot.png")
    page.evaluate("(t) => window.__handlers.resolved(t, { id: '0190-ab', url: '/scribble/api/artifacts/0190-ab/raw' })",
                  temp)
    got = _assert_inert(page)
    assert got["hrefs"] == ["/scribble/api/artifacts/0190-ab/raw"]
    assert got["srcs"] == ["/scribble/api/artifacts/0190-ab/raw"]


def test_failed_upload_shows_the_server_error_as_text(page):
    temp = _upload(page, "shot.png")
    page.evaluate("(a) => window.__handlers.failed(a[0], { status: 400, body: { error: a[1] } })", [temp, X])
    got = _assert_inert(page)
    assert got["status"] == ["Upload failed: " + X + " — Retry"]
