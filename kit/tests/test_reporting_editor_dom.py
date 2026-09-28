"""reporting-editor.js renders a hostile ProseMirror doc without running any of it (Chromium).

The editor is served publicly under ``/_kit/`` and renders docs a host hands it: scribble's embedded
``<script>`` island and ``/blocks`` fetch, bugreport's stored report bodies, and anything a host passes
to ``setDoc()`` or to the exported ``_internal.docToFragment`` walker (scribble's library page uses that
one directly). Every string field that reaches the DOM is fed markup, attribute breakouts and
``javascript:`` URLs here, down every render path, including the image upload that resolves with a URL
from the server. The page must not run any of it, must not grow an element or an ``on*`` attribute, and
must show the text verbatim.

Loads the file into a blank page through Playwright; skips when no browser binary is installed.
"""

from __future__ import annotations

import json

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

from lotek_kit.assets import asset_text  # noqa: E402

EDITOR = asset_text("reporting-editor.js")

# Every payload sets window.__xss if it ever runs.
MARKUP = [
    '"><img src=x onerror="window.__xss=(window.__xss||0)+1">',
    "'><svg onload='window.__xss=1'>",
    "</p><script>window.__xss=1</script>",
    '" onmouseover="window.__xss=1" x="',
]
BAD_URLS = [
    "javascript:window.__xss=1",
    " JaVaScRiPt:window.__xss=1",
    "java\tscript:window.__xss=1",
    "\x01javascript:window.__xss=1",
    "vbscript:window.__xss=1",
    "data:text/html,<script>parent.__xss=1</script>",
    '"><img src=x onerror=window.__xss=1>',
]


def _text(s, marks=None):
    n = {"type": "text", "text": s}
    if marks:
        n["marks"] = marks
    return n


def _hostile_doc():
    content = []
    for s in MARKUP:
        content.append({"type": "paragraph", "content": [_text(s, [{"type": "bold"}])]})
        content.append({"type": "heading", "attrs": {"level": s}, "content": [_text(s)]})
        content.append({"type": "codeBlock", "content": [_text(s)]})
        content.append({"type": "figure", "attrs": {"caption": s}, "content": [
            {"type": "inlineImage", "attrs": {"artifactId": s, "caption": s, "alt": s}}]})
        content.append({"type": "paragraph", "content": [
            {"type": "variable", "attrs": {"key": s}},
            {"type": "image", "attrs": {"src": "/a.png", "alt": s}},
            {"type": "inlineImage", "attrs": {"artifactId": "1", "alt": s, "caption": s, "src": "/b.png"}},
        ]})
        content.append({"type": s, "content": [_text(s)]})  # unknown node type
    for u in BAD_URLS:
        content.append({"type": "paragraph", "content": [_text("link " + u, [{"type": "link", "attrs": {"href": u}}])]})
        content.append({"type": "paragraph", "content": [
            {"type": "image", "attrs": {"src": u, "alt": "img"}},
            {"type": "inlineImage", "attrs": {"artifactId": "2", "src": u, "alt": "inl"}},
        ]})
    content.append({"type": "bulletList", "content": [
        {"type": "listItem", "content": [{"type": "paragraph", "content": [_text(MARKUP[0])]}]}]})
    content.append({"type": "blockquote", "content": [{"type": "paragraph", "content": [_text(MARKUP[1])]}]})
    return {"type": "doc", "content": content}


# The outbox stub records the editor's resolved/failed handlers so a test can resolve an upload with a
# server response of its choosing. It must exist before the editor script runs (it wires them at load).
_OUTBOX_STUB = """
window.__handlers = {};
window.__uploads = [];
window.LotekReportingOutbox = {
  on: function (ev, fn) { window.__handlers[ev] = fn; },
  enqueueUpload: function (op) { window.__uploads.push(op); },
  setExternalPending: function () {},
};
"""

# Inspects a rendered subtree: every attribute, every URL, the tags present, and what the page ran.
_INSPECT = """
(root) => {
  var bad = [], hrefs = [], srcs = [], tags = {};
  root.querySelectorAll("*").forEach(function (el) {
    tags[el.tagName.toLowerCase()] = 1;
    for (var i = 0; i < el.attributes.length; i++) {
      var a = el.attributes[i];
      if (/^on/i.test(a.name) || a.name === "srcdoc" || a.name === "style") bad.push(el.tagName + " " + a.name);
    }
    if (el.hasAttribute("href")) hrefs.push(el.getAttribute("href"));
    if (el.hasAttribute("src")) srcs.push(el.getAttribute("src"));
  });
  return { bad: bad, hrefs: hrefs, srcs: srcs, tags: Object.keys(tags).sort(), text: root.textContent,
           xss: window.__xss || null };
}
"""

SAFE_URL = r"^(?:https?:|mailto:|blob:|data:image/(?:png|jpeg|gif|webp);base64,|[/#?.]|[A-Za-z0-9_\-%])"
ALLOWED_TAGS = {"blockquote", "br", "code", "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6",
                "img", "li", "p", "pre", "span", "strong", "ul", "a", "div", "button", "input", "select",
                "option"}


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
    pg.set_content('<!doctype html><html><body><div id="c"></div><div id="ro"></div></body></html>')
    pg.evaluate(_OUTBOX_STUB)
    pg.add_script_tag(content=EDITOR)
    yield pg
    pg.close()
    assert errors == [], errors


def _assert_inert(page, selector):
    import re

    got = page.evaluate(_INSPECT, page.query_selector(selector))
    assert got["bad"] == [], got["bad"]
    assert set(got["tags"]) <= ALLOWED_TAGS, got["tags"]
    for url in got["hrefs"] + got["srcs"]:
        assert re.match(SAFE_URL, url), f"unsafe URL reached the DOM: {url!r}"
        assert not re.search(r"script:", url, re.I), url
    for s in MARKUP:
        assert s in got["text"], f"text not shown verbatim: {s!r}"
    return got


def _click_everything(page, selector):
    """Click every link and image. A javascript: href runs on click outside a contenteditable."""
    for el in page.query_selector_all(f"{selector} a, {selector} img"):
        try:
            el.click(timeout=500, force=True)
        except Exception:  # noqa: BLE001 - zero-size image etc.
            pass
    page.wait_for_timeout(50)
    return page.evaluate("() => window.__xss || null")


def test_mount_renders_a_hostile_doc_inertly(page):
    page.evaluate("(doc) => { window.__ed = LotekReportingEditor.mount(document.getElementById('c'), "
                  "{ doc: doc, apiBase: '/x/api' }); }", _hostile_doc())
    _assert_inert(page, "#c")
    assert _click_everything(page, "#c") is None


def test_set_doc_renders_a_hostile_doc_inertly(page):
    page.evaluate("(doc) => { window.__ed = LotekReportingEditor.mount(document.getElementById('c'), "
                  "{ doc: { type: 'doc', content: [] }, apiBase: '/x/api' }); window.__ed.setDoc(doc); }",
                  _hostile_doc())
    _assert_inert(page, "#c")
    assert _click_everything(page, "#c") is None


def test_embedded_doc_island_renders_inertly(page):
    """The <script type=application/json> island a host template embeds (scribble, bugreport)."""
    page.evaluate("""(json) => {
      var c = document.getElementById('c');
      var s = document.createElement('script');
      s.type = 'application/json'; s.className = 'lotek-reporting-editor-doc-data'; s.textContent = json;
      c.appendChild(s);
      LotekReportingEditor.mount(c, { apiBase: '/x/api' });
    }""", json.dumps(_hostile_doc()))
    _assert_inert(page, "#c")
    assert _click_everything(page, "#c") is None


def test_exported_walker_renders_inertly_outside_contenteditable(page):
    """``_internal.docToFragment`` is a public reuse point: a host may render its output read-only, where
    a link is live and a click follows it."""
    page.evaluate("(doc) => document.getElementById('ro').appendChild("
                  "LotekReportingEditor._internal.docToFragment(doc))", _hostile_doc())
    _assert_inert(page, "#ro")
    assert _click_everything(page, "#ro") is None


def test_serializer_never_saves_an_unsafe_url(page):
    """DOM -> JSON (autosave, getDoc): pasted markup carrying javascript: links/images is not persisted."""
    got = page.evaluate("""(urls) => {
      var root = document.createElement('div');
      urls.forEach(function (u) {
        var p = document.createElement('p');
        var a = document.createElement('a'); a.setAttribute('href', u); a.textContent = 'x';
        var img = document.createElement('img'); img.setAttribute('src', u);
        p.appendChild(a); p.appendChild(img); root.appendChild(p);
      });
      return JSON.stringify(LotekReportingEditor._internal.domToDoc(root));
    }""", BAD_URLS)
    assert "script:" not in got.lower()
    assert "data:text" not in got


def test_resolved_upload_url_is_not_trusted(page):
    """The inline-image upload resolves with ``data.url`` / ``data.id`` from the server: neither may put
    a script URL or markup into the editor."""
    page.evaluate("() => { window.__ed = LotekReportingEditor.mount(document.getElementById('c'), "
                  "{ doc: { type: 'doc', content: [] }, apiBase: '/x/api' }); }")
    for i, url in enumerate(BAD_URLS):
        page.evaluate("""(args) => {
          var surface = document.querySelector('#c .fr-editor-surface');
          var dt = new DataTransfer();
          dt.items.add(new File([new Uint8Array([137, 80, 78, 71])], args.name, { type: 'image/png' }));
          surface.dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
          var op = window.__uploads[window.__uploads.length - 1];
          window.__handlers.resolved(op.tempId, { id: args.id, url: args.url });
        }""", {"name": MARKUP[i % len(MARKUP)], "id": MARKUP[i % len(MARKUP)], "url": url})
    got = page.evaluate(_INSPECT, page.query_selector("#c"))
    assert got["bad"] == [], got["bad"]
    for src in got["srcs"]:
        assert not src.lower().lstrip().startswith(("javascript", "vbscript", "data:text")), src
    assert page.locator("#c .fr-editor-surface img").count() == len(BAD_URLS)
    assert _click_everything(page, "#c") is None


def test_safe_urls_still_render(page):
    """The allowlist keeps what reports actually use: http(s)/mailto/relative links, same-origin, blob:
    and raster data: images."""
    doc = {"type": "doc", "content": [
        {"type": "paragraph", "content": [
            _text("a", [{"type": "link", "attrs": {"href": "https://example.com/a?b=1#c"}}]),
            _text("b", [{"type": "link", "attrs": {"href": "http://example.com/"}}]),
            _text("c", [{"type": "link", "attrs": {"href": "mailto:a@example.com"}}]),
            _text("d", [{"type": "link", "attrs": {"href": "/scribble/x"}}]),
            _text("e", [{"type": "link", "attrs": {"href": "#frag"}}]),
            {"type": "image", "attrs": {"src": "/scribble/api/artifacts/1/raw", "alt": "i"}},
            {"type": "image", "attrs": {"src": "data:image/png;base64,iVBORw0KGgo=", "alt": "d"}},
        ]},
    ]}
    got = page.evaluate("(doc) => { var f = LotekReportingEditor._internal.docToFragment(doc);"
                        " var d = document.createElement('div'); d.appendChild(f);"
                        " return { hrefs: Array.from(d.querySelectorAll('a')).map(a => a.getAttribute('href')),"
                        " srcs: Array.from(d.querySelectorAll('img')).map(i => i.getAttribute('src')),"
                        " round: LotekReportingEditor._internal.domToDoc(d) }; }", doc)
    assert got["hrefs"] == ["https://example.com/a?b=1#c", "http://example.com/", "mailto:a@example.com",
                            "/scribble/x", "#frag"]
    assert got["srcs"] == ["/scribble/api/artifacts/1/raw", "data:image/png;base64,iVBORw0KGgo="]
    assert got["round"] == doc
