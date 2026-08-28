"""WS14: client resilience e2e (Playwright), PLAN.md §19.

Boots the real Scribble app on a background werkzeug thread (same pattern as
``scripts/capture-screenshots.py`` / ``tests/test_e2e_webui.py``), seeds a demo engagement plus one
dedicated finding with an empty evidence gallery, and drives a real Chromium instance through the
gallery's upload form while using ``page.route(...)`` to make the artifact-upload endpoint misbehave
(503s, aborted connections, delayed responses) exactly the way a crashing server or a dropped network
would.

This guards ``scribble/static/outbox.js`` (the IndexedDB-backed upload outbox) end to end:

- a transient failure (5xx / network abort) is retried with backoff until it lands, not given up on;
- a queued upload survives a page reload while the server is still unreachable (this is the whole
  reason the outbox uses IndexedDB and not an in-memory queue) and completes once it comes back;
- the shared beforeunload guard (``ScribbleOutbox.isGuardArmed()`` / ``pendingCount()``) is armed for
  as long as an upload is in flight and clears once it resolves.

Every assertion is against real end-state (docs/RAILS.md §4): a persisted ``Artifact`` row queried
straight from the database, and the gallery ``<li>``'s real ``data-id`` -- never just an HTTP 200 or the
absence of a crash.

SKIP-CLEAN: if Playwright or a browser runtime isn't available, this module skips instead of failing the
suite (mirrors ``tests/test_e2e_webui.py`` / ``scripts/capture-screenshots.py``).
"""

from __future__ import annotations

import json
import socket
import threading
import time
import uuid
from pathlib import Path

import pytest
from flask import Flask
from sqlalchemy import create_engine, select
from werkzeug.serving import make_server

import scribble
from scribble.content import schema
from scribble.enums import Severity
from scribble.models import Artifact, EngagementFinding, FindingGroup
from scribble.seed import seed_defaults
from scribble.seed.demo import seed_demo
from scribble.testing import wire_mock_host

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - exercised by whichever CI lane lacks the dep
    sync_playwright = None

# A tiny valid 1x1 PNG -- real image-header bytes so the gallery's image-vs-file branch (and the
# editor's object-URL preview path) render an actual image, not just an opaque blob.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415478da6360606000000005000166ff0f0e0000000049454e44ae426082"
)

_ARTIFACTS_ROUTE = "**/scribble/api/artifacts"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(host: str, flask_app: Flask):
    """Start a background werkzeug server on a free port, retrying a handful of fresh ports to
    dodge the tiny bind/reuse race between ``_free_port`` closing its probe socket and ``make_server``
    claiming the same port (mirrors ``tests/test_e2e_webui.py``)."""
    last_exc: OSError | None = None
    for _ in range(8):
        port = _free_port()
        try:
            server = make_server(host, port, flask_app, threaded=True)
        except OSError as exc:
            last_exc = exc
            continue
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, port
    raise last_exc if last_exc is not None else RuntimeError("could not bind a live server port")


@pytest.fixture(scope="module")
def live_app(tmp_path_factory):
    """Boot a real Scribble app (demo-seeded + one dedicated, artifact-free finding for these tests)
    on a background werkzeug server, and tear it down after the module's tests finish."""
    tmp = tmp_path_factory.mktemp("scribble-e2e-resilience")
    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = "e2e-resilience-test"
    engine = create_engine(f"sqlite:///{tmp / 'e2e.db'}", future=True)
    cfg = scribble.register(
        flask_app, engine, instance_path=str(tmp), base_template="scribble/base.html"
    )
    # The demo shell supplies a mock host: scribble persists evidence only to an object store,
    # and this fixture boots a REAL server, so without one every upload in this module fails.
    wire_mock_host(cfg)

    with cfg.session_factory() as session:
        seed_defaults(session)
        engagement = seed_demo(session)

        # A dedicated group/finding with NO pre-attached evidence, so any `.scribble-gallery-item`
        # that appears during a test is unambiguously the one *that test* uploaded (docs/RAILS.md §4:
        # fixtures must be able to reveal the defect, not launder it behind pre-existing rows).
        group = FindingGroup(engagement=engagement, name="Resilience QA", order_index=999)
        session.add(group)
        session.flush()
        finding = EngagementFinding(
            engagement=engagement,
            group=group,
            title="Resilience QA Finding",
            severity=Severity.high,
            order_index=0,
            content_json={"description": schema.doc_from_text("Resilience QA fixture.")},
        )
        session.add(finding)
        session.commit()
        ids = {"engagement_id": engagement.id, "finding_id": finding.id}

    host = "127.0.0.1"
    server, thread, port = _serve(host, flask_app)
    try:
        yield {"base_url": f"http://{host}:{port}", "session_factory": cfg.session_factory, **ids}
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    if sync_playwright is None:
        pytest.skip("playwright is not installed; skipping browser e2e (skip-clean, see docs/RAILS.md)")
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - any launch failure -> skip-clean, never fail the suite
            pytest.skip(f"no usable Chromium runtime ({exc}); skipping browser e2e (skip-clean)")
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser):
    # Browser.new_page() creates an isolated ad-hoc context per call, so each test gets its own fresh
    # IndexedDB/localStorage -- no cross-test outbox state leaks between tests in this module.
    p = browser.new_page(viewport={"width": 1280, "height": 900})
    try:
        yield p
    finally:
        p.close()


# --------------------------------------------------------------------------------- helpers


def _write_png(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(_PNG_BYTES)
    return path


def _artifact_rows(session_factory, finding_id: int, filename: str) -> list[Artifact]:
    with session_factory() as session:
        stmt = select(Artifact).where(
            Artifact.finding_id == finding_id, Artifact.filename == filename
        )
        return list(session.scalars(stmt))


def _wait_for_artifact_row(page, session_factory, finding_id: int, filename: str, timeout: float = 10.0):
    """Poll the DB for the persisted row, alternating with the *page's* own connection so any
    outstanding ``page.route()`` interception actually gets serviced while we wait.

    Playwright's Python sync API only delivers a queued ``Fetch.requestPaused`` (i.e. invokes a
    ``page.route()`` handler for a request already in flight) while the main thread is inside an
    active call into the page/browser connection -- a bare ``time.sleep()`` with zero Playwright calls
    leaves those deliveries queued indefinitely (verified directly: a JS `setTimeout`-triggered
    `fetch()` against a routed URL sits paused through 25s of plain `time.sleep()`, then resolves within
    one `page.wait_for_timeout()` call). So this helper's wait is `page.wait_for_timeout()`, not
    `time.sleep()`, specifically so the outbox's scheduled retry `fetch()` actually gets a chance to be
    intercepted and answered by the test's route handler instead of sitting paused in the browser.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = _artifact_rows(session_factory, finding_id, filename)
        if rows:
            return rows[0]
        page.wait_for_timeout(150)
    return None


def _open_finding(page, live_app) -> None:
    finding_id = live_app["finding_id"]
    resp = page.goto(f"{live_app['base_url']}/scribble/findings/{finding_id}")
    assert resp is not None and resp.status == 200


def _gallery(page, live_app):
    finding_id = live_app["finding_id"]
    gallery = page.locator(f'.scribble-gallery[data-finding-id="{finding_id}"]')
    assert gallery.count() == 1
    return gallery


def _upload_via_gallery(page, gallery, png_path: Path) -> None:
    gallery.locator(".scribble-gallery-file").set_input_files(str(png_path))
    gallery.locator(".scribble-gallery-upload button[type=submit]").click()


def _real_item_by_filename(gallery, filename: str):
    """The gallery accumulates real, persisted rows across every test in this module (``live_app`` is
    module-scoped, so earlier tests' uploads are still sitting in the same finding's gallery). A bare
    ``.scribble-gallery-item[data-id]`` locator matches *any* of them -- scope to this test's own
    filename so "the real row landed" can't be satisfied by a different test's leftover artifact."""
    return gallery.locator(".scribble-gallery-item[data-id]", has_text=filename)


def _block_doc(session_factory, finding_id: int, block: str):
    """The persisted ProseMirror doc for one content block, straight from the DB (autosave writes it)."""
    with session_factory() as session:
        finding = session.get(EngagementFinding, finding_id)
        return (finding.content_json or {}).get(block) if finding is not None else None


def _is_real_artifact_id(value) -> bool:
    """A server-assigned artifact id: a parseable UUID (or a legacy int), never a placeholder."""
    if isinstance(value, int) and not isinstance(value, bool):
        return True
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _inline_images(doc) -> list[dict]:
    """Every ``inlineImage`` node anywhere in a ProseMirror doc (recursive)."""
    found: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "inlineImage":
                found.append(node)
            for child in node.get("content", []) or []:
                walk(child)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc)
    return found


def _poll(page, fn, timeout: float = 10.0):
    """Poll ``fn()`` until it returns a truthy value, pumping the *page's* connection between checks so
    any paused ``page.route()`` interception (e.g. the outbox's retry POST) actually gets serviced --
    see ``_wait_for_artifact_row``'s docstring for why a bare ``time.sleep`` would stall route delivery."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        page.wait_for_timeout(150)
    return fn()


# --------------------------------------------------------------------------------- tests


def test_upload_retries_on_transient_failure_then_persists(page, live_app, tmp_path):
    """Fail the upload POST twice (503, then an aborted connection), then let the third attempt
    through. The optimistic preview must appear immediately (before any of that resolves), the outbox
    must actually retry (proven by counting real requests reaching the route), and the artifact must
    ultimately land as a real, persisted row -- not just a 200 on *some* request."""
    finding_id = live_app["finding_id"]
    filename = "retry-evidence.png"
    png_path = _write_png(tmp_path, filename)

    attempts = {"count": 0}

    def handle(route):
        if route.request.method != "POST":
            route.continue_()
            return
        attempts["count"] += 1
        if attempts["count"] == 1:
            route.fulfill(status=503, json={"error": "simulated server crash"})
        elif attempts["count"] == 2:
            route.abort("failed")  # simulated dropped connection
        else:
            route.continue_()

    page.route(_ARTIFACTS_ROUTE, handle)
    try:
        _open_finding(page, live_app)
        gallery = _gallery(page, live_app)
        _upload_via_gallery(page, gallery, png_path)

        # The optimistic preview shows up right away, well before the retries have a chance to
        # resolve -- proves paste/drop is never silently swallowed while the server is unreachable.
        pending = gallery.locator(".scribble-gallery-item.is-pending")
        pending.first.wait_for(state="attached", timeout=2000)

        # Eventually the outbox's retry succeeds and the row is reconciled to a real artifact id.
        real_item = _real_item_by_filename(gallery, filename)
        real_item.first.wait_for(state="attached", timeout=15000)
        assert real_item.count() == 1
        assert gallery.locator(".scribble-gallery-item.is-pending").count() == 0
        assert gallery.locator(".scribble-gallery-item.is-failed").count() == 0

        # The guard this test exists to prove: retries actually happened. If the outbox's retry logic
        # were removed (a single failed attempt just gave up), attempts would freeze at 1 and the real
        # row would never appear -- the wait_for above would already have timed out.
        assert attempts["count"] >= 3

        # Real end-state (docs/RAILS.md §4): exactly one persisted Artifact row, not an HTTP proxy.
        rows = _artifact_rows(live_app["session_factory"], finding_id, filename)
        assert len(rows) == 1
        assert rows[0].finding_id == finding_id
    finally:
        page.unroute(_ARTIFACTS_ROUTE, handle)


def test_upload_survives_reload_while_offline_then_flushes(page, live_app, tmp_path):
    """Abort every upload attempt (simulated offline), reload the page while still offline (the op
    must survive in IndexedDB, not an in-memory-only queue), then go back online and confirm the
    auto-flush-on-load retry completes the upload."""
    finding_id = live_app["finding_id"]
    filename = "offline-evidence.png"
    png_path = _write_png(tmp_path, filename)

    blocked = {"value": True}
    attempts = {"count": 0}

    def handle(route):
        if route.request.method != "POST":
            route.continue_()
            return
        attempts["count"] += 1
        if blocked["value"]:
            route.abort("failed")
        else:
            route.continue_()

    page.route(_ARTIFACTS_ROUTE, handle)
    try:
        _open_finding(page, live_app)
        gallery = _gallery(page, live_app)
        _upload_via_gallery(page, gallery, png_path)

        gallery.locator(".scribble-gallery-item.is-pending").first.wait_for(
            state="attached", timeout=2000
        )
        # Give the outbox at least one real failed attempt before we pull the rug out from under it
        # with a reload -- otherwise this test could pass even with no durable persistence at all. Use
        # page.wait_for_timeout(), not time.sleep(): see _wait_for_artifact_row's docstring -- a bare
        # time.sleep() here would leave the route interception for the outbox's retry fetch() queued
        # and never delivered, since nothing would be pumping the page's connection while we wait.
        deadline = time.time() + 5.0
        while attempts["count"] < 1 and time.time() < deadline:
            page.wait_for_timeout(100)
        assert attempts["count"] >= 1

        # Reload while still "offline" -- the queued op only survives this if it's durably stored
        # (IndexedDB), not held purely in a JS variable that dies with the old page.
        page.reload()
        _gallery(page, live_app)  # page re-rendered fine; (no pre-existing artifact row yet)
        assert _artifact_rows(live_app["session_factory"], finding_id, filename) == []

        # Now "reconnect": let subsequent attempts through. auto-flush-on-load already re-armed a
        # retry loop for the durably-queued op the instant this reloaded page's outbox.js ran.
        blocked["value"] = False

        row = _wait_for_artifact_row(page, live_app["session_factory"], finding_id, filename, timeout=15.0)
        assert row is not None, (
            "artifact never persisted after reconnecting -- the outbox likely lost the queued op on "
            "reload (this guard fails if IndexedDB persistence is removed in favor of an in-memory "
            "queue, per PLAN.md §19)"
        )

        # Confirm the DOM reconciles too: a fresh reload now renders the persisted row server-side.
        page.reload()
        gallery = _gallery(page, live_app)
        item = _real_item_by_filename(gallery, filename)
        item.first.wait_for(state="attached", timeout=5000)
        assert filename in page.content()
    finally:
        page.unroute(_ARTIFACTS_ROUTE, handle)


def test_beforeunload_guard_armed_while_pending_and_cleared_after_flush(page, live_app, tmp_path):
    """beforeunload dialogs can't be asserted directly in a headless browser, so assert the
    observable state ScribbleOutbox exposes instead: pendingCount()/isGuardArmed() must be truthy
    while an upload is in flight, and both must clear once it resolves."""
    filename = "guard-evidence.png"
    png_path = _write_png(tmp_path, filename)

    # Reuses the same fast, non-blocking abort-then-continue shape as
    # test_upload_retries_on_transient_failure_then_persists (the outbox's own backoff is what keeps
    # the row "pending" for a window) rather than a Python-side blocking delay: a route handler that
    # blocks synchronously for a while (time.sleep, or an Event awaited in the callback itself) was
    # observed to make Playwright's page.click() hang for that entire duration and then return only
    # once the request finally completed -- collapsing the very "still pending" window this test needs
    # to observe. Handlers here return immediately, so click() returns immediately too.
    attempts = {"count": 0}

    def handle(route):
        if route.request.method != "POST":
            route.continue_()
            return
        attempts["count"] += 1
        if attempts["count"] == 1:
            route.abort("failed")
        else:
            route.continue_()

    page.route(_ARTIFACTS_ROUTE, handle)
    try:
        _open_finding(page, live_app)

        # Before any upload: nothing pending, guard not armed.
        assert page.evaluate("window.ScribbleOutbox.pendingCount()") == 0
        assert page.evaluate("window.ScribbleOutbox.isGuardArmed()") is False

        gallery = _gallery(page, live_app)
        _upload_via_gallery(page, gallery, png_path)

        gallery.locator(".scribble-gallery-item.is-pending").first.wait_for(
            state="attached", timeout=5000
        )
        assert page.evaluate("window.ScribbleOutbox.pendingCount()") > 0
        assert page.evaluate("window.ScribbleOutbox.isGuardArmed()") is True

        real_item = _real_item_by_filename(gallery, filename)
        real_item.first.wait_for(state="attached", timeout=10000)

        assert page.evaluate("window.ScribbleOutbox.pendingCount()") == 0
        assert page.evaluate("window.ScribbleOutbox.isGuardArmed()") is False
        assert attempts["count"] >= 2  # the first attempt really did fail and get retried
    finally:
        page.unroute(_ARTIFACTS_ROUTE, handle)


def test_editor_inline_paste_transient_fail_then_persists_with_real_artifact_id(page, live_app, tmp_path):
    """W5 / C1 guard: an inline image upload that transiently fails then succeeds must end up in the
    finding's persisted ``content_json`` as an ``inlineImage`` node carrying a REAL ``artifactId`` --
    AND while the upload is pending/failed the doc must never contain an ``artifactId``-less
    ``inlineImage`` (which would bake a blank image into the finding + every report).

    This fails against the pre-fix editor for two independent reasons (both are the C1 bug): the inline
    upload always 400s because ``engagement_id`` was never sent (so it never resolves to a real id), and
    the optimistic preview node it inserted serialized into ``content_json`` as a blank ``inlineImage``
    the moment any autosave ran. It passes only once inline paste threads ``engagement_id`` through and
    keeps the transient preview out of the serialized doc until the upload resolves.
    """
    finding_id = live_app["finding_id"]
    block = "description"
    marker = "INLINEMARKER_XYZ"
    png_path = _write_png(tmp_path, "inline-evidence.png")

    attempts = {"count": 0}

    def handle(route):
        if route.request.method != "POST":
            route.continue_()
            return
        attempts["count"] += 1
        if attempts["count"] == 1:
            route.fulfill(status=503, json={"error": "simulated server crash"})
        else:
            route.continue_()  # succeeds only if the client actually sent engagement_id (C1)

    page.route(_ARTIFACTS_ROUTE, handle)
    try:
        _open_finding(page, live_app)

        editor = page.locator(f'.scribble-editor-wrap[data-block="{block}"]')
        assert editor.count() == 1
        editor.locator(".fr-editor-surface").wait_for(state="attached", timeout=5000)

        # Drive the editor's own inline-image upload path (the toolbar's hidden file input calls the
        # exact same uploadAndInsertImage() as paste/drop).
        editor.locator('.fr-editor-toolbar input[type="file"]').set_input_files(str(png_path))

        # Force an autosave WHILE the upload is still in flight, by typing into the block. If the
        # transient preview were serializable (the pre-fix bug), this save would persist a blank
        # inlineImage.
        surface = editor.locator(".fr-editor-surface")
        surface.click()
        page.keyboard.type(marker)

        # A save carrying the typed marker must land (autosave POST is not intercepted).
        saved = _poll(
            page,
            lambda: marker in json.dumps(_block_doc(live_app["session_factory"], finding_id, block)),
            timeout=10.0,
        )
        assert saved, "autosave with the typed marker never persisted"

        # Invariant while pending/just-resolving: no artifactId-less inlineImage was ever serialized.
        mid = _block_doc(live_app["session_factory"], finding_id, block)
        assert not any(
            (img.get("attrs") or {}).get("artifactId") is None for img in _inline_images(mid)
        ), f"a blank inlineImage (no artifactId) was persisted: {_inline_images(mid)}"

        # The upload ultimately resolves; the real inlineImage (with a real artifactId) lands in the doc.
        def has_real_inline():
            doc = _block_doc(live_app["session_factory"], finding_id, block)
            imgs = _inline_images(doc)
            # A REAL artifactId is a UUID string since lotek#335 (it was an int). The property under
            # test is unchanged: the node carries a server-assigned id, not a placeholder.
            aid = (imgs[0].get("attrs") or {}).get("artifactId") if imgs else None
            return imgs if _is_real_artifact_id(aid) else None

        imgs = _poll(page, has_real_inline, timeout=15.0)
        assert imgs, "no inlineImage with a real (UUID) artifactId ever persisted"
        artifact_id = imgs[0]["attrs"]["artifactId"]
        # A server-assigned id, not a client placeholder — a UUID since lotek#335 (was an int).
        assert _is_real_artifact_id(artifact_id), artifact_id

        # Final end-state: exactly the real inline image, and never a blank one.
        final = _block_doc(live_app["session_factory"], finding_id, block)
        final_imgs = _inline_images(final)
        assert len(final_imgs) >= 1
        assert all((img.get("attrs") or {}).get("artifactId") for img in final_imgs)

        # And a real Artifact row backs it.
        with live_app["session_factory"]() as session:
            assert session.get(Artifact, artifact_id) is not None
        assert attempts["count"] >= 2  # transiently failed once, then really succeeded
    finally:
        page.unroute(_ARTIFACTS_ROUTE, handle)


def test_outbox_gives_up_after_max_attempts_and_drains(page, live_app, tmp_path):
    """W3 guard: a permanently-failing (always-5xx) upload must NOT retry forever. After the attempt
    cap the op is dropped, the row is marked failed, and -- critically -- ``pendingCount()`` returns to
    0 and the beforeunload guard clears (otherwise a doomed upload pins the guard on for the whole
    session and the op never leaves IndexedDB). A tiny capped config keeps the test fast."""
    # Must be set before any page script loads so outbox.js reads it at module-eval time.
    page.add_init_script(
        "window.__scribbleOutboxConfig = { maxAttempts: 3, baseDelayMs: 20, maxDelayMs: 40 };"
    )
    finding_id = live_app["finding_id"]
    filename = "doomed-evidence.png"
    png_path = _write_png(tmp_path, filename)

    attempts = {"count": 0}

    def handle(route):
        if route.request.method != "POST":
            route.continue_()
            return
        attempts["count"] += 1
        route.fulfill(status=503, json={"error": "always down"})

    page.route(_ARTIFACTS_ROUTE, handle)
    try:
        _open_finding(page, live_app)
        gallery = _gallery(page, live_app)
        _upload_via_gallery(page, gallery, png_path)

        # The row ends up FAILED rather than spinning forever.
        gallery.locator(".scribble-gallery-item.is-failed").first.wait_for(
            state="attached", timeout=10000
        )
        # The whole point of the cap: the queue drains and the guard clears.
        assert _poll(page, lambda: page.evaluate("window.ScribbleOutbox.pendingCount()") == 0)
        assert page.evaluate("window.ScribbleOutbox.isGuardArmed()") is False
        assert attempts["count"] == 3  # exactly the cap, then it gave up (not unbounded)

        # And nothing was persisted.
        assert _artifact_rows(live_app["session_factory"], finding_id, filename) == []
    finally:
        page.unroute(_ARTIFACTS_ROUTE, handle)
