"""WS10 layer B: browser page-render smoke test (Playwright), PLAN.md #13/#9.

Boots the real Fraction app in a background werkzeug thread (same pattern as
``scripts/capture-screenshots.py``), seeds the demo dataset (``fraction.seed.demo.seed_demo``) plus one
small deterministic fixture of our own (a manually-ordered group with two DISTINCT severities), and
drives a real Chromium instance through the five key pages: dashboard, library, engagement board, a
finding, and the HTML report. It also exercises one real UI action -- clicking "Re-rank by severity" --
and asserts the visible order actually changes, not just that the click didn't crash.

SKIP-CLEAN (LOTEK-style): if Playwright or a browser runtime isn't available, this module skips instead
of failing the suite -- mirrors ``scripts/capture-screenshots.py``'s guard and docs/RAILS.md's "never
fail a pipeline on a missing browser" rule.
"""

from __future__ import annotations

import socket
import threading

import pytest
from flask import Flask
from sqlalchemy import create_engine
from werkzeug.serving import make_server

import fraction
from fraction.artifacts_storage import save_bytes
from fraction.content import schema
from fraction.enums import ArtifactKind, ArtifactPlacement, OrderMode, Severity
from fraction.models import Artifact, EngagementFinding, FindingGroup
from fraction.seed import seed_defaults
from fraction.seed.demo import seed_demo

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - exercised by whichever CI lane lacks the dep
    sync_playwright = None

# A tiny valid 1x1 PNG -- real image-header bytes so the artifact renders as an image thumbnail.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415478da6360606000000005000166ff0f0e0000000049454e44ae426082"
)

_GALLERY_FILENAME = "qa-gallery-evidence.png"


def _serve(host: str, flask_app: Flask):
    """Start a background werkzeug server on a free port, hardened against the tiny bind/reuse race
    between ``_free_port`` closing its probe socket and ``make_server`` claiming the same port: retry a
    handful of fresh ports before giving up."""
    last_exc: OSError | None = None
    for _ in range(8):
        port = _free_port()
        try:
            server = make_server(host, port, flask_app, threaded=True)
        except OSError as exc:  # port got grabbed in the race window -- try another
            last_exc = exc
            continue
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, port
    raise last_exc if last_exc is not None else RuntimeError("could not bind a live server port")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_app(tmp_path_factory):
    """Boot a real Fraction app (demo-seeded + a small rerank fixture) on a background werkzeug
    server, and tear it down after the module's tests finish."""
    tmp = tmp_path_factory.mktemp("fraction-e2e-webui")
    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = "e2e-webui-test"
    engine = create_engine(f"sqlite:///{tmp / 'e2e.db'}", future=True)
    cfg = fraction.register(
        flask_app, engine, instance_path=str(tmp), base_template="fraction/base.html"
    )

    with cfg.session_factory() as session:
        seed_defaults(session)
        engagement = seed_demo(session)

        # A deterministic fixture layered on top of the demo data: two DISTINCT severities in a
        # MANUAL order that disagrees with severity order, so clicking "Re-rank by severity" has a
        # real, provable effect instead of a same-severity fixture where nothing visibly changes
        # (docs/RAILS.md #4 -- fixtures must be able to reveal the defect).
        group = FindingGroup(
            engagement=engagement, name="Rerank QA", order_index=999, order_mode=OrderMode.manual
        )
        session.add(group)
        session.flush()
        low = EngagementFinding(
            engagement=engagement,
            group=group,
            title="QA Low Severity Finding",
            severity=Severity.low,
            order_index=0,
            content_json={"description": schema.doc_from_text("Low severity QA fixture.")},
        )
        critical = EngagementFinding(
            engagement=engagement,
            group=group,
            title="QA Critical Finding",
            severity=Severity.critical,
            order_index=1,
            content_json={"description": schema.doc_from_text("Critical QA fixture.")},
        )
        session.add(low)
        session.add(critical)
        session.flush()

        # Attach a real artifact to the Low finding so the finding page's evidence gallery has an
        # actual row to render (not just the empty "No evidence attached yet." shell) -- lets the
        # finding-page test assert the gallery, not merely that the page loaded.
        storage_path, _sha, _size = save_bytes(cfg, engagement.id, _GALLERY_FILENAME, _PNG_BYTES)
        session.add(
            Artifact(
                engagement=engagement,
                finding=low,
                kind=ArtifactKind.screenshot,
                placement=ArtifactPlacement.attached,
                filename=_GALLERY_FILENAME,
                content_type="image/png",
                storage_path=storage_path,
                caption="QA gallery evidence",
                order_index=0,
                include_in_report=True,
            )
        )
        session.commit()
        ids = {
            "engagement_id": engagement.id,
            "group_id": group.id,
            "low_finding_id": low.id,
            "critical_finding_id": critical.id,
        }

    host = "127.0.0.1"
    server, thread, port = _serve(host, flask_app)
    try:
        yield {"base_url": f"http://{host}:{port}", **ids}
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
    p = browser.new_page(viewport={"width": 1440, "height": 900})
    try:
        yield p
    finally:
        p.close()


# --------------------------------------------------------------------------------- five key pages


def test_dashboard_renders(page, live_app):
    resp = page.goto(f"{live_app['base_url']}/fraction/")
    assert resp is not None and resp.status == 200
    assert page.locator("text=Recent engagements").count() > 0
    assert page.locator(".stats-grid .stat").count() >= 1


def test_library_renders(page, live_app):
    resp = page.goto(f"{live_app['base_url']}/fraction/library")
    assert resp is not None and resp.status == 200
    assert page.locator("text=Vulnerability templates").count() > 0
    assert page.locator("table.table tbody tr").count() > 0  # seeded templates present


def test_engagement_board_renders_group_cards(page, live_app):
    eng_id = live_app["engagement_id"]
    resp = page.goto(f"{live_app['base_url']}/fraction/engagements/{eng_id}")
    assert resp is not None and resp.status == 200
    assert page.locator(".fraction-board-group").count() >= 1
    group_id = live_app["group_id"]
    assert page.locator(f'.fraction-board-group[data-group-id="{group_id}"]').count() == 1


def test_finding_page_renders_editor_and_gallery(page, live_app):
    finding_id = live_app["low_finding_id"]
    resp = page.goto(f"{live_app['base_url']}/fraction/findings/{finding_id}")
    assert resp is not None and resp.status == 200

    # Editor block present.
    assert page.locator('[data-block="description"]').count() >= 1

    # Gallery present AND populated with the artifact the fixture attached -- a real evidence row
    # (data-id), its filename, and its caption. Asserting on the actual item (not just the gallery
    # shell) is what makes this a gallery test rather than a bare page-load check.
    gallery = page.locator(f'.fraction-gallery[data-finding-id="{finding_id}"]')
    assert gallery.count() == 1
    item = gallery.locator(".fraction-gallery-item[data-id]")
    assert item.count() == 1
    assert item.first.is_visible()
    assert _GALLERY_FILENAME in page.content()
    assert "QA gallery evidence" in page.content()


def test_report_page_renders_risk_banner(page, live_app):
    eng_id = live_app["engagement_id"]
    resp = page.goto(f"{live_app['base_url']}/fraction/engagements/{eng_id}/report")
    assert resp is not None and resp.status == 200
    assert page.locator(".risk .level").count() >= 1
    assert "Overall Risk" in page.content()


# --------------------------------------------------------------------------- one real UI action


def test_rerank_by_severity_button_changes_the_visible_order(page, live_app):
    """The one real UI action: click "Re-rank by severity" on the QA fixture group (manually ordered
    as [Low, Critical] -- the reverse of severity order) and assert the board visibly reorders to
    [Critical, Low] once board.js reloads the page, proving the click did more than not crash."""
    eng_id = live_app["engagement_id"]
    group_id = live_app["group_id"]
    low_id = live_app["low_finding_id"]
    crit_id = live_app["critical_finding_id"]

    page.goto(f"{live_app['base_url']}/fraction/engagements/{eng_id}")

    findings_list = page.locator(f'ul.fraction-board-findings[data-group-id="{group_id}"]')
    before_ids = findings_list.locator(".fraction-board-finding").evaluate_all(
        "els => els.map(e => e.dataset.findingId)"
    )
    assert before_ids == [str(low_id), str(crit_id)]  # the manual order the fixture seeded

    rerank_btn = page.locator(
        f'.fraction-board-group[data-group-id="{group_id}"] .fraction-board-group-rerank'
    )
    assert not rerank_btn.is_disabled()  # order_mode=manual -> the button is live, not greyed out

    with page.expect_navigation():
        rerank_btn.click()

    findings_list_after = page.locator(f'ul.fraction-board-findings[data-group-id="{group_id}"]')
    after_ids = findings_list_after.locator(".fraction-board-finding").evaluate_all(
        "els => els.map(e => e.dataset.findingId)"
    )
    assert after_ids == [str(crit_id), str(low_id)]  # worst-first now
    assert after_ids != before_ids  # the order actually changed, not a no-op click
