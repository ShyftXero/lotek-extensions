"""Coverage-panel presentation guards (ShyftXero/lotek-extensions#44).

The client-reported defect: on the engagement board's coverage panel, every per-item status control
rendered as a **native, unstyled `<select>`** — a white/light-grey box with OS chrome — against the dark
themed panel, one per checklist item, plus an empty dashed rectangle above the list.

Two things make this hard to guard with the usual "does the class have a rule?" audit:

1. `.ckp-status` *had* a rule (`font-size` + `min-width`), so a class-coverage audit passed. Only the
   rendered *result* shows the defect — hence the browser tests below assert **computed style**, using
   the note field in the same row as the oracle (it was themed correctly all along, so "the select looks
   like its neighbour" is a property the panel should always have).
2. The panel's CSS was not reaching every context. `scribble/static/scribble.css` is linked by
   scribble's OWN base template only; a scribble MOUNTED in lotek renders inside lotek's `base.html`,
   whose single stylesheet-injection point is `head_extra` — which no scribble template filled. So on
   the host NOTHING in scribble.css applied and the whole panel was unstyled, which is the state the
   client actually saw. The panel's rules therefore live in their own stylesheet
   (`static/checklists_panel.css`) that the *pages* link through `head_extra`, and the tests below pin
   that the link survives into a HOST-shaped page as well as a standalone one.

The hermetic tests parse CSS with a deliberately small regex: comments are stripped first (a class named
only in a comment must never count as "defined"), then each rule is read as (selector-list,
declarations). Nested `@media` wrappers fall out of the scan — their inner rules are still matched
individually — which is fine, nothing here depends on a media query.

Honest limit: the browser tests drive the STANDALONE app, the only shell this repo can boot. They prove
the plumbing and the rendering; that the same link lands in a real lotek page is pinned hermetically by
`test_mounted_page_links_the_panel_stylesheet` against a base template shaped like the host's.
"""

from __future__ import annotations

import re
import socket
import threading
from pathlib import Path

import pytest
from flask import Flask
from sqlalchemy import create_engine, select
from werkzeug.serving import make_server

import scribble
from scribble.checklists import assign_template
from scribble.models import ChecklistTemplate, Client, Engagement
from scribble.seed import seed_defaults

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - exercised by whichever lane lacks the dep
    sync_playwright = None

_PKG = Path(scribble.__file__).resolve().parent
_PANEL_CSS = _PKG / "static" / "checklists_panel.css"
_SCRIBBLE_CSS = _PKG / "static" / "scribble.css"
_CHECKLISTS_JS = _PKG / "static" / "checklists.js"

_PANEL_CSS_LINK = "checklists_panel.css"

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.S)

# A host base template shaped like lotek's (src/app/templates/base.html): the ONLY place a mounted
# extension can add a stylesheet is the `head_extra` block. Deliberately minimal — if scribble's page
# templates stop reaching this hook, these tests fail the same way production did.
_HOST_BASE = """<!doctype html>
<html data-theme="dark" lang="en">
<head>
  <title>{% block title %}Lotek | Datahaven{% endblock %}</title>
  <link rel="stylesheet" href="/static/styles.css" />
  {% block head_extra %}{% endblock %}
</head>
<body>
  <div class="appbar-title">{% block appbar_title %}{{ self.title() }}{% endblock %}</div>
  <div class="appbar-actions">{% block appbar_actions %}{% endblock %}</div>
  <div class="content">{% block content %}{% endblock %}</div>
  {% block body_extra %}{% endblock %}
</body>
</html>
"""


def _rules(css: str) -> list[tuple[str, str]]:
    """Flat (selector-list, declarations) pairs, comments removed. See the module docstring."""
    return [(sel.strip(), body) for sel, body in _RULE_RE.findall(_COMMENT_RE.sub(" ", css))]


def _bodies_for(css: str, class_name: str) -> list[str]:
    """Declaration bodies of every rule whose selector list mentions `.class_name`."""
    token = re.compile(r"\." + re.escape(class_name) + r"(?![\w-])")
    return [body for sel, body in _rules(css) if token.search(sel)]


def _declares(bodies: list[str], prop: str) -> bool:
    pat = re.compile(r"(?:^|;|\s)" + re.escape(prop) + r"\s*:", re.S)
    return any(pat.search(b) for b in bodies)


def _style_blocks(html: str) -> str:
    return "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S))


def _stylesheet_links(html: str) -> list[str]:
    return [ln for ln in re.findall(r"<link[^>]*>", html) if "stylesheet" in ln]


def _new_engagement(session_factory) -> object:
    with session_factory() as db:
        e = Engagement(name="Theming Eng")
        db.add(e)
        db.commit()
        return e.id


def _engagement_page(client, session_factory) -> str:
    resp = client.get(f"/scribble/engagements/{_new_engagement(session_factory)}")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


@pytest.fixture
def mounted_client(tmp_path):
    """Scribble mounted the way lotek mounts it: `base_template="base.html"`, where that base is the
    HOST's and offers `head_extra` as its only stylesheet hook."""
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "base.html").write_text(_HOST_BASE)
    app = Flask(__name__, template_folder=str(templates))
    app.config["SECRET_KEY"] = "mounted-test"
    engine = create_engine(f"sqlite:///{tmp_path / 'mounted.db'}", future=True)
    cfg = scribble.register(app, engine, instance_path=str(tmp_path), base_template="base.html")
    with cfg.session_factory() as db:
        seed_defaults(db)
        db.commit()
    return app.test_client(), cfg.session_factory


# ────────────────────────── hermetic: the rules exist, and they reach both shells ───────────────────


def test_panel_stylesheet_themes_the_status_control():
    """`.ckp-status` must carry real theming, not just sizing: a `<select>` with no background/colour/
    border paints the native OS control the client reported."""
    bodies = _bodies_for(_PANEL_CSS.read_text(), "ckp-status")
    assert bodies, f".ckp-status has no rule in {_PANEL_CSS.name}"
    for prop in ("background", "color", "border"):
        assert _declares(bodies, prop), f".ckp-status declares no {prop}: {bodies!r}"


def test_panel_stylesheet_keeps_the_tray_hidden_when_hidden():
    """`.ckp-tray { display: flex }` is an AUTHOR rule, so it beats the UA's `[hidden]{display:none}` and
    the tray stayed on screen as an empty dashed rectangle even though the markup ships `hidden` and the
    JS toggles that attribute. Any rule that sets `display` on the tray therefore owes a `[hidden]`
    override."""
    css = _PANEL_CSS.read_text()
    if not _declares(_bodies_for(css, "ckp-tray"), "display"):
        pytest.skip("no rule sets display on .ckp-tray, so [hidden] is not being defeated")
    overrides = [
        body
        for sel, body in _rules(css)
        if re.search(r"\.ckp-tray\[hidden\]", sel) and re.search(r"display\s*:\s*none", body)
    ]
    assert overrides, ".ckp-tray sets display but has no .ckp-tray[hidden]{display:none} override"


def test_panel_rules_have_a_single_home():
    """One source of truth: the panel's classes must not be re-declared in scribble.css (where they used
    to live). Two copies is how the standalone and mounted renderings drifted apart in the first place."""
    strays = {
        cls
        for sel, _body in _rules(_SCRIBBLE_CSS.read_text())
        for cls in re.findall(r"\.(ckp-[A-Za-z0-9_-]+|ck-[A-Za-z0-9_-]+)", sel)
    }
    assert not strays, f"panel classes re-declared in scribble.css: {sorted(strays)}"


def test_engagement_page_links_the_panel_stylesheet(client, session_factory):
    links = _stylesheet_links(_engagement_page(client, session_factory))
    assert any(_PANEL_CSS_LINK in ln for ln in links), f"panel stylesheet not linked: {links}"


def test_mounted_page_links_the_panel_stylesheet(mounted_client):
    """The guard for the defect as the CLIENT met it: rendered inside the HOST's base template, the
    engagement page must still ship the panel's stylesheet. scribble.css is NOT linked there (the host
    injects nothing on an extension's behalf), so without this the whole panel is unstyled on prod."""
    test_client, session_factory = mounted_client
    html = _engagement_page(test_client, session_factory)
    links = _stylesheet_links(html)
    assert any(_PANEL_CSS_LINK in ln for ln in links), f"mounted page lost the panel stylesheet: {links}"
    assert not any("scribble.css" in ln for ln in links), (
        "a host page must not link scribble.css — it redefines :root/body/.card/.btn and would restyle "
        f"lotek's own shell: {links}"
    )


def test_checklists_library_page_links_the_panel_stylesheet(client):
    """The library page renders `.ckp-kind` rows, so it owes the same stylesheet."""
    html = client.get("/scribble/checklists").get_data(as_text=True)
    links = _stylesheet_links(html)
    assert any(_PANEL_CSS_LINK in ln for ln in links), f"panel stylesheet not linked: {links}"


def test_panel_stylesheet_is_served(client):
    assert client.get(f"/scribble/static/{_PANEL_CSS_LINK}").status_code == 200


def test_every_panel_class_the_js_emits_has_a_css_rule(client, session_factory):
    """Drift guard: every `ckp-`/`ck-` class `checklists.js` puts in the DOM must be defined in the CSS
    the panel actually loads. This is what catches a class that reads as intent but styles nothing —
    `ckp-tmpl` was exactly that."""
    js = _CHECKLISTS_JS.read_text()
    emitted: set[str] = set()
    buckets = re.findall(r'\["([a-z_]+)",\s*"[^"]*"\]', js)
    for literal in re.findall(r'el\(\s*"[a-z]+"\s*,\s*"([^"]+)"', js):
        for tok in literal.split():
            if not tok.startswith(("ckp-", "ck-")):
                continue  # host-owned classes (.btn, .muted) are not this panel's business
            if tok.endswith("-"):  # a concatenation prefix, e.g. "ck-" + bucket name
                emitted.update(tok + b for b in buckets)
            else:
                emitted.add(tok)
    assert "ckp-status" in emitted, "regex found no classes — the extraction broke, not the CSS"
    assert {"ck-satisfied", "ck-open"} <= emitted, f"bucket expansion broke: {sorted(emitted)}"

    loaded = (
        _PANEL_CSS.read_text(),
        _SCRIBBLE_CSS.read_text(),
        _style_blocks(_engagement_page(client, session_factory)),
    )
    defined = {cls for css in loaded for sel, _b in _rules(css) for cls in re.findall(r"\.([\w-]+)", sel)}
    assert not (emitted - defined), f"classes emitted with no CSS rule: {sorted(emitted - defined)}"


# ──────────────────────────── browser: what the client actually sees ───────────────────────────────


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def panel_app(tmp_path_factory):
    """A real scribble on a background werkzeug server with one coverage checklist assigned — the same
    shape as the issue's repro script, so the screenshots and these assertions describe one thing."""
    tmp = tmp_path_factory.mktemp("scribble-ckp-theming")
    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = "ckp-theming-test"
    engine = create_engine(f"sqlite:///{tmp / 'ckp.db'}", future=True)
    cfg = scribble.register(
        flask_app, engine, instance_path=str(tmp), base_template="scribble/base.html"
    )
    with cfg.session_factory() as db:
        seed_defaults(db)
        db.commit()
    with cfg.session_factory() as db:
        c = Client(name="Theming Client")
        db.add(c)
        db.flush()
        eng = Engagement(name="Theming board", client_id=c.id, company_name="Theming Co")
        db.add(eng)
        db.commit()
        tmpl = db.scalars(select(ChecklistTemplate).limit(1)).first()
        assert tmpl is not None, "seed_defaults shipped no checklist template to assign"
        assign_template(db, eng, tmpl, assigned_by="test")
        db.commit()
        eid = eng.id

    last: OSError | None = None
    for _ in range(8):
        try:
            server = make_server("127.0.0.1", _free_port(), flask_app, threaded=True)
        except OSError as exc:  # port stolen in the bind race — try another
            last = exc
            continue
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield {"url": f"http://127.0.0.1:{server.server_port}/scribble/engagements/{eid}"}
        finally:
            server.shutdown()
            thread.join(timeout=5)
        return
    raise last if last is not None else RuntimeError("could not bind a live server port")


@pytest.fixture(scope="module")
def panel_page(panel_app):
    if sync_playwright is None:
        pytest.skip("playwright not installed; skipping browser checks (skip-clean)")
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - any launch failure is a skip, never a suite failure
            pytest.skip(f"no usable Chromium runtime ({exc}); skipping browser checks (skip-clean)")
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        try:
            page.goto(panel_app["url"], wait_until="networkidle")
            page.wait_for_selector("#checklist-panel .ckp-status")
            yield page
        finally:
            browser.close()


def test_status_select_matches_the_field_next_to_it(panel_page):
    """The rendered defect: a native light-grey select. Oracle is the `.ckp-note` input in the same row —
    it was themed correctly all along, so the select should paint the same field background/ink."""
    styles = panel_page.evaluate(
        """() => {
             const pick = el => { const c = getComputedStyle(el);
               return {bg: c.backgroundColor, fg: c.color, bw: c.borderTopWidth}; };
             return {sel: pick(document.querySelector('#checklist-panel .ckp-status')),
                     note: pick(document.querySelector('#checklist-panel .ckp-note'))};
           }"""
    )
    sel, note = styles["sel"], styles["note"]
    assert sel["bg"] == note["bg"], f"status control background {sel['bg']} != note {note['bg']}"
    assert sel["fg"] == note["fg"], f"status control colour {sel['fg']} != note {note['fg']}"
    assert sel["bw"] not in ("0px", ""), f"status control has no border: {sel!r}"
    # The measured native control before the fix; kept as an explicit pin on the visible symptom.
    assert sel["bg"] not in ("rgb(255, 255, 255)", "rgb(239, 239, 239)"), f"native control: {sel!r}"


def test_assign_tray_is_invisible_until_opened_and_closes_again(panel_page):
    """The empty dashed rectangle: the tray ships `hidden` and the JS toggles that attribute, so it must
    be display:none closed — and must actually close again on the second click."""
    tray = "#ckp-assign-tray"
    display = "el => getComputedStyle(el).display"
    assert panel_page.eval_on_selector(tray, display) == "none", "hidden tray is still displayed"
    panel_page.click("#ckp-assign-btn")
    panel_page.wait_for_function(
        "() => !document.querySelector('#ckp-assign-tray').hidden", timeout=5000
    )
    assert panel_page.eval_on_selector(tray, display) != "none", "opened tray is not displayed"
    panel_page.click("#ckp-assign-btn")
    assert panel_page.eval_on_selector(tray, display) == "none", "tray did not close again"


def test_panel_rows_lay_out_beside_their_status_control(panel_page):
    """The mounted panel lost `.ckp-item`'s flex layout entirely (select stacked ABOVE the item text
    instead of beside it). Pin the geometry, not the rule: the control and the text share a row."""
    boxes = panel_page.evaluate(
        """() => {
             const row = document.querySelector('#checklist-panel .ckp-item');
             const b = el => { const r = el.getBoundingClientRect(); return {x: r.x, y: r.y, h: r.height}; };
             return {sel: b(row.querySelector('.ckp-status')), text: b(row.querySelector('.ckp-item-text'))};
           }"""
    )
    sel, text = boxes["sel"], boxes["text"]
    assert text["x"] > sel["x"], f"item text is not to the right of the control: {boxes!r}"
    same_row = abs(text["y"] - sel["y"]) < max(sel["h"], text["h"])
    assert same_row, f"control and text are not on one row: {boxes!r}"
