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

The browser tests run in BOTH shells. `panel_page` boots the standalone app; `mounted_panel_page` boots
the same board against `_HOST_BASE` — scribble's pages extending a base shaped like lotek's, where
scribble.css is never linked — because that, not standalone, is where the layout was actually lost.

Honest limit: `_HOST_BASE` is a stand-in, and its host serves no stylesheet of its own, so these tests
cannot see a HOST rule that FIGHTS the panel; only the panel failing to supply its own. The real
base.html + styles.css were driven by hand (ext#44, review round 2 notes in the plan file).
"""

from __future__ import annotations

import re
import socket
import threading
from contextlib import contextmanager
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
_CKLIB_JS = _PKG / "static" / "checklists_library.js"

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


def test_mounted_library_page_still_ships_its_cklib_rules(mounted_client):
    """`.cklib-*` (library page only) lives in scribble.css, which the HOST's base never links — so by the
    reasoning above it should be unstyled when mounted. Measured: it is not, because
    `checklists_library.html` hardcodes `{% extends "scribble/base.html" %}` and therefore renders in
    scribble's own shell even on the host, dragging scribble.css along.

    So this guards the REACHABILITY, not the hardcode: switch that page to `scribble_base` (the filed
    follow-up) and the rules silently stop arriving — which is the moment they need moving into a
    page-linked stylesheet, the same treatment `.ckp-*` got. Fail then, loudly."""
    test_client, _ = mounted_client
    html = test_client.get("/scribble/checklists").get_data(as_text=True)

    emitted = {
        tok
        for literal in (
            re.findall(r'class="([^"]*)"', html)
            + re.findall(r'el\(\s*"[a-z]+"\s*,\s*"([^"]+)"', _CKLIB_JS.read_text())
        )
        for tok in literal.split()
        if tok.startswith("cklib-")
    }
    assert {"cklib-card", "cklib-head"} <= emitted, f"extraction broke, not the CSS: {sorted(emitted)}"

    served = [_style_blocks(html)]
    for link in _stylesheet_links(html):
        href = re.search(r'href="([^"]+)"', link)
        if not href:
            continue
        resp = test_client.get(href.group(1))
        if resp.status_code == 200:  # the host's own /static/styles.css is absent in this fixture
            served.append(resp.get_data(as_text=True))
    defined = {cls for css in served for sel, _b in _rules(css) for cls in re.findall(r"\.([\w-]+)", sel)}

    missing = emitted - defined
    assert not missing, (
        "the mounted library page renders classes no stylesheet it links defines: "
        f"{sorted(missing)} — move them out of scribble.css into a page-linked sheet (ext#44 review)"
    )


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


def _boot(tmp, name: str, *, mounted: bool = False):
    """A real scribble on its own sqlite, seeded. Returns (flask_app, session_factory).

    `mounted=False` is the standalone shell: pages extend `scribble/base.html`, which links scribble.css.
    `mounted=True` is how PRODUCTION runs it: pages extend the HOST's `base.html` (`_HOST_BASE`), whose
    only stylesheet hook is `head_extra` and which links nothing on the extension's behalf. The host's
    own `/static/styles.css` is deliberately absent (404) so that anything the panel renders correctly in
    this shell was rendered by the panel's OWN stylesheet and by nothing else.
    """
    if mounted:
        templates = tmp / f"{name}.templates"
        templates.mkdir()
        (templates / "base.html").write_text(_HOST_BASE)
        flask_app = Flask(__name__, template_folder=str(templates), static_folder=None)
        base_template = "base.html"
    else:
        flask_app = Flask(__name__)
        base_template = "scribble/base.html"
    flask_app.config["SECRET_KEY"] = "ckp-theming-test"
    engine = create_engine(f"sqlite:///{tmp / name}", future=True)
    cfg = scribble.register(flask_app, engine, instance_path=str(tmp), base_template=base_template)
    with cfg.session_factory() as db:
        seed_defaults(db)
        db.commit()
    return flask_app, cfg.session_factory


def _board_with_checklist(session_factory):
    """One engagement with one coverage checklist assigned — the same shape as the issue's repro script,
    so the screenshots and these assertions describe one thing. Returns the engagement id."""
    with session_factory() as db:
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
        return eng.id


@contextmanager
def _serving(flask_app):
    """Run `flask_app` on a background werkzeug server, yielding its base URL."""
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
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join(timeout=5)
        return
    raise last if last is not None else RuntimeError("could not bind a live server port")


@pytest.fixture(scope="module")
def panel_app(tmp_path_factory):
    """The board in scribble's OWN shell."""
    tmp = tmp_path_factory.mktemp("scribble-ckp-theming")
    flask_app, session_factory = _boot(tmp, "ckp.db")
    eid = _board_with_checklist(session_factory)
    with _serving(flask_app) as base:
        yield {"url": f"{base}/scribble/engagements/{eid}"}


@pytest.fixture(scope="module")
def mounted_panel_app(tmp_path_factory):
    """The identical board MOUNTED — rendered inside a host-shaped base, which is what prod does and the
    only shell in which the panel ever lost its layout. Its own app + db, so it shares no state with the
    standalone lane."""
    tmp = tmp_path_factory.mktemp("scribble-ckp-mounted-browser")
    flask_app, session_factory = _boot(tmp, "mounted.db", mounted=True)
    eid = _board_with_checklist(session_factory)
    with _serving(flask_app) as base:
        yield {"url": f"{base}/scribble/engagements/{eid}"}


@pytest.fixture(scope="module")
def no_templates_app(tmp_path_factory):
    """Same board, but every checklist template hidden — exactly what the library page's per-template
    "Hide" button does, and what `/checklists/templates/suggest` then answers with two empty lists."""
    tmp = tmp_path_factory.mktemp("scribble-ckp-empty")
    flask_app, session_factory = _boot(tmp, "empty.db")
    with session_factory() as db:
        eng = Engagement(name="Empty tray board")
        db.add(eng)
        rows = db.scalars(select(ChecklistTemplate)).all()
        assert rows, "seed_defaults shipped no templates, so hiding them proves nothing"
        for t in rows:
            t.hidden = True
        db.commit()
        eid = eng.id
    with _serving(flask_app) as base:
        yield {"url": f"{base}/scribble/engagements/{eid}"}


@pytest.fixture(scope="module")
def browser():
    if sync_playwright is None:
        pytest.skip("playwright not installed; skipping browser checks (skip-clean)")
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - any launch failure is a skip, never a suite failure
            pytest.skip(f"no usable Chromium runtime ({exc}); skipping browser checks (skip-clean)")
        try:
            yield b
        finally:
            b.close()


@pytest.fixture(scope="module")
def panel_page(browser, panel_app):
    page = browser.new_page(viewport={"width": 1400, "height": 1000})
    try:
        page.goto(panel_app["url"], wait_until="networkidle")
        page.wait_for_selector("#checklist-panel .ckp-status")
        yield page
    finally:
        page.close()


@pytest.fixture(scope="module")
def mounted_panel_page(browser, mounted_panel_app):
    page = browser.new_page(viewport={"width": 1400, "height": 1000})
    try:
        page.goto(mounted_panel_app["url"], wait_until="networkidle")
        page.wait_for_selector("#checklist-panel .ckp-status")
        yield page
    finally:
        page.close()


@pytest.fixture(scope="module")
def empty_tray_page(browser, no_templates_app):
    page = browser.new_page(viewport={"width": 1400, "height": 1000})
    try:
        page.goto(no_templates_app["url"], wait_until="networkidle")
        page.wait_for_selector("#ckp-assign-btn")
        yield page
    finally:
        page.close()


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


def test_assign_tray_never_opens_as_an_empty_dashed_box(empty_tray_page):
    """The other half of #44's dashed rectangle, and the half `[hidden]` does NOT cover: with every
    template hidden, `suggest` answers two empty lists, so the tray opens with zero children and paints
    the identical empty dashed box — no rows, no reason, button reads as broken. Assert the OPEN tray is
    either still not displayed or carries a visible explanation; an empty-but-displayed tray fails."""
    tray = "#ckp-assign-tray"
    assert empty_tray_page.eval_on_selector(tray, "el => getComputedStyle(el).display") == "none"
    empty_tray_page.click("#ckp-assign-btn")
    empty_tray_page.wait_for_function(
        "() => { const t = document.querySelector('#ckp-assign-tray');"
        "        return !t.hidden || t.children.length; }",
        timeout=5000,
    )
    state = empty_tray_page.eval_on_selector(
        tray,
        """el => ({display: getComputedStyle(el).display, text: el.innerText.trim(),
                  h: Math.round(el.getBoundingClientRect().height)})""",
    )
    if state["display"] == "none":
        return  # left closed instead of opened empty — also a correct answer
    assert state["text"], f"tray opened with no content: {state!r}"
    assert empty_tray_page.is_visible(f"{tray} .ckp-tray-empty"), f"message is not visible: {state!r}"
    # The measured symptom: an 890x18 dashed sliver with nothing in it.
    assert state["h"] > 18, f"tray is still the empty-height dashed sliver: {state!r}"


@pytest.mark.parametrize("shell", ["panel_page", "mounted_panel_page"])
def test_panel_rows_lay_out_beside_their_status_control(request, shell):
    """`.ckp-item`'s flex row must survive into BOTH shells: the control and the text share a row.

    The two lanes are not equal evidence, and the difference is the point (ext#44 review round 2). The
    STANDALONE lane never showed this defect — scribble.css always carried `.ckp-item { display: flex }`
    and scribble's own base always linked it — so that lane is a no-regression pin and nothing more. The
    MOUNTED lane is where the layout was actually lost: the host's base links scribble.css nowhere, so
    before this branch the row had no flex at all and the select stacked ABOVE its item text (measured on
    lotek's real base.html: sel y=657, text y=677 — same x). Pin the geometry, not the rule, because
    "the rule exists somewhere" is exactly the check that passed while the client's screen was broken."""
    page = request.getfixturevalue(shell)
    boxes = page.evaluate(
        """() => {
             const row = document.querySelector('#checklist-panel .ckp-item');
             const b = el => { const r = el.getBoundingClientRect(); return {x: r.x, y: r.y, h: r.height}; };
             return {sel: b(row.querySelector('.ckp-status')), text: b(row.querySelector('.ckp-item-text'))};
           }"""
    )
    sel, text = boxes["sel"], boxes["text"]
    assert text["x"] > sel["x"], f"[{shell}] item text is not to the right of the control: {boxes!r}"
    same_row = abs(text["y"] - sel["y"]) < max(sel["h"], text["h"])
    assert same_row, f"[{shell}] control and text are not on one row: {boxes!r}"
