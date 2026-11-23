"""WS10 layer B: browser page-render smoke test (Playwright), PLAN.md #13/#9.

Boots the real Scribble app in a background werkzeug thread (same pattern as
``scripts/capture-screenshots.py``), seeds the demo dataset (``scribble.seed.demo.seed_demo``) plus one
small deterministic fixture of our own (a manually-ordered group with two DISTINCT severities), and
drives a real Chromium instance through the five key pages: dashboard, library, engagement board, a
finding, and the HTML report. It also exercises two real UI actions -- clicking "Re-rank by severity",
and the destructive "Un-adopt + delete findings…" control (ext#402) -- and asserts the visible result
actually changed, not just that the click didn't crash.

SKIP-CLEAN (LOTEK-style): if Playwright or a browser runtime isn't available, this module skips instead
of failing the suite -- mirrors ``scripts/capture-screenshots.py``'s guard and docs/RAILS.md's "never
fail a pipeline on a missing browser" rule.
"""

from __future__ import annotations

import json
import socket
import threading

import pytest
from flask import Flask
from sqlalchemy import create_engine, select
from werkzeug.serving import make_server

import scribble
from scribble.content import schema
from scribble.enums import ArtifactKind, ArtifactPlacement, OrderMode, Severity
from scribble.models import Artifact, BoardFinding, FindingGroup, ReportBoard
from scribble.seed import seed_defaults
from scribble.seed.demo import seed_demo
from scribble.testing import register_kit_assets_shim, store_evidence, wire_mock_host

# The MOUNTED-host bundle, wired exactly as `app/extensions.py::_inject_host` wires it in core (its own
# docstring says so). Reused rather than re-hand-rolled: this test's whole point is that the page works
# under the real host contract, so a second, subtly different stub here would prove the wrong thing.
from tests.conftest import FakeFindingDTO, StubHost, _wire_stub_host

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
    """Boot a real Scribble app (demo-seeded + a small rerank fixture) on a background werkzeug
    server, and tear it down after the module's tests finish."""
    tmp = tmp_path_factory.mktemp("scribble-e2e-webui")
    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = "e2e-webui-test"
    engine = create_engine(f"sqlite:///{tmp / 'e2e.db'}", future=True)
    cfg = scribble.register(
        flask_app, engine, instance_path=str(tmp), base_template="scribble/base.html"
    )
    # THE demo shell, and the reason `scribble.testing` is shipped rather than test-only: standalone
    # Scribble persists evidence to the host's object store like every other configuration, so the
    # thing that boots it supplies a mock host. Keeping a disk fallback "just for standalone" is what
    # put evidence in two places to begin with.
    wire_mock_host(cfg)
    # Standalone has no core to register the kit's asset blueprint, and the templates load the
    # shared reporting editor from it -- so this REAL browser would otherwise get a BuildError.
    register_kit_assets_shim(flask_app)

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
        low = BoardFinding(
            engagement=engagement,
            group=group,
            title="QA Low Severity Finding",
            severity=Severity.low,
            order_index=0,
            content_json={"description": schema.doc_from_text("Low severity QA fixture.")},
        )
        critical = BoardFinding(
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
        storage_path, _sha, _size = store_evidence(flask_app, _GALLERY_FILENAME, _PNG_BYTES)
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
        # session_factory is exposed so a test can assert what the UI actually PERSISTED (e.g. the
        # prose dedup storing NULL), not just what the page re-renders.
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
    p = browser.new_page(viewport={"width": 1440, "height": 900})
    try:
        yield p
    finally:
        p.close()


# --------------------------------------------------------------------------------- five key pages


def test_dashboard_renders(page, live_app):
    resp = page.goto(f"{live_app['base_url']}/scribble/")
    assert resp is not None and resp.status == 200
    assert page.locator("text=Recent engagements").count() > 0
    assert page.locator(".stats-grid .stat").count() >= 1


def test_library_renders(page, live_app):
    resp = page.goto(f"{live_app['base_url']}/scribble/library")
    assert resp is not None and resp.status == 200
    assert page.locator("text=Vulnerability templates").count() > 0
    assert page.locator("table.table tbody tr").count() > 0  # seeded templates present


def test_engagement_board_renders_group_cards(page, live_app):
    eng_id = live_app["engagement_id"]
    resp = page.goto(f"{live_app['base_url']}/scribble/engagements/{eng_id}")
    assert resp is not None and resp.status == 200
    assert page.locator(".scribble-board-group").count() >= 1
    group_id = live_app["group_id"]
    assert page.locator(f'.scribble-board-group[data-group-id="{group_id}"]').count() == 1


def test_finding_page_renders_editor_and_gallery(page, live_app):
    finding_id = live_app["low_finding_id"]
    resp = page.goto(f"{live_app['base_url']}/scribble/findings/{finding_id}")
    assert resp is not None and resp.status == 200

    # Editor block present.
    assert page.locator('[data-block="description"]').count() >= 1

    # Gallery present AND populated with the artifact the fixture attached -- a real evidence row
    # (data-id), its filename, and its caption. Asserting on the actual item (not just the gallery
    # shell) is what makes this a gallery test rather than a bare page-load check.
    gallery = page.locator(f'.scribble-gallery[data-finding-id="{finding_id}"]')
    assert gallery.count() == 1
    item = gallery.locator(".scribble-gallery-item[data-id]")
    assert item.count() == 1
    assert item.first.is_visible()
    assert _GALLERY_FILENAME in page.content()
    assert "QA gallery evidence" in page.content()


def test_report_page_renders_risk_banner(page, live_app):
    eng_id = live_app["engagement_id"]
    resp = page.goto(f"{live_app['base_url']}/scribble/engagements/{eng_id}/report")
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

    page.goto(f"{live_app['base_url']}/scribble/engagements/{eng_id}")

    findings_list = page.locator(f'ul.scribble-board-findings[data-group-id="{group_id}"]')
    before_ids = findings_list.locator(".scribble-board-finding").evaluate_all(
        "els => els.map(e => e.dataset.findingId)"
    )
    assert before_ids == [str(low_id), str(crit_id)]  # the manual order the fixture seeded

    rerank_btn = page.locator(
        f'.scribble-board-group[data-group-id="{group_id}"] .scribble-board-group-rerank'
    )
    assert not rerank_btn.is_disabled()  # order_mode=manual -> the button is live, not greyed out

    with page.expect_navigation():
        rerank_btn.click()

    findings_list_after = page.locator(f'ul.scribble-board-findings[data-group-id="{group_id}"]')
    after_ids = findings_list_after.locator(".scribble-board-finding").evaluate_all(
        "els => els.map(e => e.dataset.findingId)"
    )
    assert after_ids == [str(crit_id), str(low_id)]  # worst-first now
    assert after_ids != before_ids  # the order actually changed, not a no-op click


# -------------------------------------- destructive un-adopt in the real browser (ext#402 verification)
#
# The engagement, the two adopted jobs and their rows are INPUTS here -- simulated state, never a
# simulated response: the preview JSON, the confirm text, the DOM and the DB effects below are all the
# real app's own output, produced by the real routes and the real board.js.

_ALPHA_JOB = "job-alpha-web"
_BETA_JOB = "job-beta-db"
_ALPHA_TITLES = ("Alpha RCE on web-01", "Alpha XSS on web-02")
_ALPHA_NOTE = f"Scan coverage: job {_ALPHA_JOB}"
_BETA_TITLE = "Beta RCE on db-01"
_BETA_NOTE = f"Scan coverage: job {_BETA_JOB}"
_MANUAL_TITLE = "Operator-authored note (kept)"


@pytest.fixture(scope="module")
def unadopt_app(tmp_path_factory):
    """A MOUNTED-host Scribble app with one engagement holding TWO adopted jobs plus a hand-authored
    finding -- the fixture ext#402's verification needs and the demo dataset does not have.

    Mounting is load-bearing: `host_is_mounted()` is what puts the tenancy gate in front of every board
    route and what makes the Source-jobs panel (and therefore the destructive control) render at all.
    `scribble.testing.wire_mock_host` deliberately does NOT set `extras['host']` (see its comment), so
    the rest of the bundle is wired by `tests.conftest._wire_stub_host`, which supplies the
    `can_view_client` view hook too. Without that hook `can_view_client_id` fails CLOSED and every page
    here would 404 -- so it is wired, not worked around.

    Rows are shaped after `promote.finding_is_enriched`: a promoted scan row carries `source_finding_id`
    (one of the job's own ids from `host.findings().list_findings`, which `_job_finding_ids` reads) and
    NO `source_job_id` -- that column is the #656 coverage note's alone (its partial unique index is on
    `(engagement_id, source_job_id)`, so a job owns at most ONE such row). The note carries
    `source_job_id` with NO `source_finding_id`; a hand-authored row carries neither. The two jobs are
    registered on the host's reverse index (`list_jobs`, keyed by the engagement) because that is what
    `_is_source_job` consults.
    """
    tmp = tmp_path_factory.mktemp("scribble-e2e-unadopt")
    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = "e2e-unadopt-test"
    engine = create_engine(f"sqlite:///{tmp / 'unadopt.db'}", future=True)
    cfg = scribble.register(
        flask_app, engine, instance_path=str(tmp), base_template="scribble/base.html"
    )
    wire_mock_host(cfg)  # still needed: evidence persistence is host-only by design
    stub = StubHost()
    _wire_stub_host(cfg, stub)
    register_kit_assets_shim(flask_app)

    # The forward half of the contract: the scan finding ids each job produced.
    stub.findings.add_job(_ALPHA_JOB, owner_id=1, dtos=[
        FakeFindingDTO(id=101, title=_ALPHA_TITLES[0], job_id=_ALPHA_JOB),
        FakeFindingDTO(id=102, title=_ALPHA_TITLES[1], job_id=_ALPHA_JOB),
    ])
    stub.findings.add_job(_BETA_JOB, owner_id=1, dtos=[
        FakeFindingDTO(id=201, title=_BETA_TITLE, job_id=_BETA_JOB),
    ])

    with cfg.session_factory() as session:
        seed_defaults(session)
        engagement = ReportBoard(name="Un-adopt QA (ext#402)")
        session.add(engagement)
        session.flush()

        # The reverse half: both jobs ARE adopted into this engagement, so the panel lists them and
        # `_is_source_job` answers True for the destroy (the link still exists before the click).
        stub.add_promoted_job(engagement.id, _ALPHA_JOB, name="Alpha scan")
        stub.add_promoted_job(engagement.id, _BETA_JOB, name="Beta scan")

        alpha_rows = [
            BoardFinding(engagement=engagement, title=t, severity=Severity.high, source_finding_id=sid)
            for t, sid in zip(_ALPHA_TITLES, (101, 102), strict=True)
        ]
        beta_row = BoardFinding(engagement=engagement, title=_BETA_TITLE, severity=Severity.high,
                                source_finding_id=201)
        alpha_note = BoardFinding(engagement=engagement, title=_ALPHA_NOTE, severity=Severity.info,
                                  source_job_id=_ALPHA_JOB)
        beta_note = BoardFinding(engagement=engagement, title=_BETA_NOTE, severity=Severity.info,
                                 source_job_id=_BETA_JOB)
        # The hand-authored finding: no job id, no source finding id -> nothing may remove it.
        manual = BoardFinding(engagement=engagement, title=_MANUAL_TITLE, severity=Severity.low)
        for row in [*alpha_rows, beta_row, alpha_note, beta_note, manual]:
            session.add(row)
        session.flush()
        ids = {
            "alpha": [str(f.id) for f in alpha_rows],
            "alpha_note": str(alpha_note.id),
            "beta": str(beta_row.id),
            "beta_note": str(beta_note.id),
            "manual": str(manual.id),
            "engagement_id": str(engagement.id),
        }
        session.commit()

    host = "127.0.0.1"
    server, thread, port = _serve(host, flask_app)
    try:
        yield {"base_url": f"http://{host}:{port}", "session_factory": cfg.session_factory,
               "stub": stub, "ids": ids}
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_unadopt_destroy_removes_only_the_adopted_block_in_the_real_ui(page, unadopt_app):
    """ext#402's Verification, on the real UI through the real routes.

    Issue #402: "Un-adopt + delete findings" reported success while the adopted block stayed on the
    board. Its Verification asks for the request, the confirmation fields, the response, the resulting
    DB state and the refreshed DOM -- for BOTH un-adopt options, on an engagement with a second job in
    the same section and a hand-authored finding.

    ONE test, both options, on one engagement:
      A. "Un-adopt + delete findings…" for job-alpha  -> only alpha's rows (and its coverage note) go;
         the operator sees alpha's titles in the confirm BEFORE accepting; the link is cleared once.
      B. "Un-adopt (keep findings)" for job-beta      -> the link is cleared, NOTHING is deleted.

    What it would do if the guard were wrong in the PERMISSIVE direction: a destroy that deleted too
    much fails the beta/manual survival assertions, and one that deleted nothing (the #402 defect) fails
    the alpha-is-gone assertions on BOTH the DOM and the DB. Neither arm can pass vacuously.
    """
    base = unadopt_app["base_url"]
    ids = unadopt_app["ids"]
    stub = unadopt_app["stub"]
    eng_id = ids["engagement_id"]

    def board_url() -> str:
        return f"{base}/scribble/engagements/{eng_id}"

    def rendered_finding_ids() -> list[str]:
        return page.locator(".scribble-board-finding").evaluate_all(
            "els => els.map(e => e.dataset.findingId)"
        )

    def db_rows() -> list[tuple[str, object]]:
        with unadopt_app["session_factory"]() as db:
            rows = db.scalars(
                select(BoardFinding).where(BoardFinding.engagement_id == eng_id)
            ).all()
            return sorted((f.title, f.source_finding_id) for f in rows)

    page.goto(board_url())

    # ---- BEFORE: both jobs' blocks and the manual finding are on the board and in the DB.
    before_dom = rendered_finding_ids()
    before_db = db_rows()
    assert set(ids["alpha"]) <= set(before_dom)
    assert {ids["beta"], ids["manual"]} <= set(before_dom)
    assert len(before_dom) == 6, before_dom
    assert sorted(t for t, _ in before_db) == sorted(
        [*_ALPHA_TITLES, _ALPHA_NOTE, _BETA_TITLE, _BETA_NOTE, _MANUAL_TITLE]
    )

    destroy_btn = page.locator(
        f'.scribble-unadopt-destroy[data-destroy-url*="/unadopt-job/{_ALPHA_JOB}/"]'
    )
    assert destroy_btn.count() == 1
    preview_url = base + destroy_btn.get_attribute("data-preview-url")
    destroy_url = base + destroy_btn.get_attribute("data-destroy-url")
    assert preview_url.endswith(f"/unadopt-job/{_ALPHA_JOB}/preview")
    assert destroy_url.endswith(f"/unadopt-job/{_ALPHA_JOB}/destroy")

    # ---- A. the destructive option, in TWO passes through the real control.
    #
    # Pass 1 clicks and CANCELS the confirmation -- which is the safe default -- so the preview's own
    # JSON can be read while the document is still on this page (a navigation makes Chromium discard a
    # finished fetch's body: `Response.json()` then raises "No resource with given identifier found",
    # which is a property of the body's lifetime, not of anything under test). Pass 2 clicks and ACCEPTS.
    dialogs: list[str] = []
    accept_next = {"value": False}

    def on_dialog(dialog):
        dialogs.append(dialog.message)  # the CONFIRMATION FIELDS the operator decides on
        if accept_next["value"]:
            dialog.accept()
        else:
            dialog.dismiss()

    responses: list[tuple[str, int]] = []
    page.on("dialog", on_dialog)
    page.on("response", lambda r: responses.append((r.url, r.status)))

    # Both waits are needed and neither implies the other: the response event fires when the fetch
    # lands, the dialog only once board.js's `.then` runs, so waiting on the response alone can read
    # `dialogs` before it has been appended to.
    with page.expect_response(lambda r: r.url == preview_url) as preview_info, \
            page.expect_event("dialog"):
        destroy_btn.click()

    preview = preview_info.value
    preview_body = preview.json()
    assert preview.status == 200
    assert preview_body["job_id"] == _ALPHA_JOB
    # The preview lists exactly what the destroy removes: the two scan rows AND the job's coverage note
    # (promote.finding_is_enriched's second arm -- a note left behind would claim a job still fed a
    # board whose rows are gone). Nothing from job-beta and not the author's note.
    assert sorted(f["title"] for f in preview_body["findings"]) == sorted(
        [*_ALPHA_TITLES, _ALPHA_NOTE]
    )
    assert set(f["id"] for f in preview_body["findings"]) == {*ids["alpha"], ids["alpha_note"]}
    assert len(dialogs) == 1, dialogs

    # Cancelling is a no-op on every surface: no row deleted, no link cleared, block still rendered.
    assert db_rows() == before_db
    assert set(before_dom) <= set(rendered_finding_ids())
    assert not stub.unadopt_calls

    # Pass 2: the operator accepts. board.js re-enables the button in `.finally` after the dialog, so
    # wait for the control to be live again rather than sleeping.
    accept_next["value"] = True
    page.wait_for_function(
        "sel => { const b = document.querySelector(sel); return !!b && !b.disabled; }",
        arg=f'.scribble-unadopt-destroy[data-destroy-url*="/unadopt-job/{_ALPHA_JOB}/"]',
    )
    with page.expect_navigation():
        destroy_btn.click()

    destroy_statuses = [st for url, st in responses if url == destroy_url]
    assert destroy_statuses == [302], (destroy_url, responses)
    assert page.url == board_url()
    # (1) DOM after the action: alpha's block is gone; beta's finding and the author's note remain.
    after_dom = rendered_finding_ids()
    assert not (set(ids["alpha"]) & set(after_dom)), "the adopted block survived the destroy"
    assert ids["alpha_note"] not in after_dom
    assert {ids["beta"], ids["manual"]} <= set(after_dom)
    assert len(after_dom) == len(before_dom) - 3, after_dom
    # ...and the panel no longer offers a control for alpha, while beta's is still there.
    assert page.locator(
        f'.scribble-unadopt-destroy[data-destroy-url*="/unadopt-job/{_ALPHA_JOB}/"]'
    ).count() == 0
    assert page.locator(
        f'.scribble-unadopt-destroy[data-destroy-url*="/unadopt-job/{_BETA_JOB}/"]'
    ).count() == 1

    # (2) DB after the action: exactly alpha's two rows + its coverage note were deleted (3 of 6).
    after_db = db_rows()
    assert sorted(t for t, _ in after_db) == sorted([_BETA_TITLE, _BETA_NOTE, _MANUAL_TITLE])
    assert {sid for _, sid in after_db} == {201, None}, after_db

    # (3) The confirmation named exactly what was removed -- an operator deciding blind is the other
    #     half of this defect, so the text is asserted, not just "a dialog appeared".
    assert len(dialogs) == 2, dialogs
    assert dialogs[1] == dialogs[0], "the premise changed between Cancel and Accept"
    confirm = dialogs[1]
    assert "Delete 3 finding(s)" in confirm
    for title in [*_ALPHA_TITLES, _ALPHA_NOTE]:
        assert title in confirm
    assert _BETA_TITLE not in confirm
    assert _MANUAL_TITLE not in confirm
    # The bullets the operator reads name the SAME set the preview returned -- two surfaces, one
    # predicate. Parsed from the dialog text rather than asserted title-by-title, so a preview that
    # listed a DIFFERENT set from the confirm (the drift #402 is about) cannot pass.
    bullets = [
        line.strip().removeprefix("•").strip()
        for line in confirm.splitlines() if line.strip().startswith("•")
    ]
    assert set(bullets) == {f["title"] for f in preview_body["findings"]}

    # (4) The link was cleared exactly once, for exactly this job.
    assert [call[0] for call in stub.unadopt_calls] == [_ALPHA_JOB]

    # ---- B. the link-only option, through the same panel: findings stay, link goes.
    with page.expect_navigation():
        page.locator(f'form[action$="/unadopt-job/{_BETA_JOB}"] button[type="submit"]').click()

    assert page.url == board_url()
    assert {ids["beta"], ids["manual"]} <= set(rendered_finding_ids())
    assert sorted(t for t, _ in db_rows()) == sorted([_BETA_TITLE, _BETA_NOTE, _MANUAL_TITLE])
    # Both options recorded exactly one link-clear each, one per job -- the destructive one did not
    # clear beta's link, and this one deleted nothing.
    assert [call[0] for call in stub.unadopt_calls] == [_ALPHA_JOB, _BETA_JOB]

    # ---- C. THE #402 trigger: destroy a job whose LINK is already gone.
    # This is the state the defect was reported in (a link-only un-adopt, or an adopt that poured rows
    # and then failed before stamping the link). `_is_source_job` is now False for beta, and it must
    # scope only the link-clear: pre-fix the route returned 302 having deleted nothing, leaving the
    # block on the board. The panel must still offer the control here -- that came from the branch's
    # own panel fix -- so the click proves both halves.
    beta_destroy = page.locator(
        f'.scribble-unadopt-destroy[data-destroy-url*="/unadopt-job/{_BETA_JOB}/"]'
    )
    assert beta_destroy.count() == 1, "no control reaches the destroy route once the link is gone"
    beta_destroy_url = base + beta_destroy.get_attribute("data-destroy-url")

    with page.expect_navigation():
        beta_destroy.click()

    beta_statuses = [st for url, st in responses if url == beta_destroy_url]
    assert beta_statuses == [302], (beta_destroy_url, responses)
    # The block is gone on every surface and the author's note -- the only hand-authored row left --
    # survives; the link was already cleared, so the host is NOT asked a third time.
    assert db_rows() == [(_MANUAL_TITLE, None)]
    assert rendered_finding_ids() == [ids["manual"]]
    assert [call[0] for call in stub.unadopt_calls] == [_ALPHA_JOB, _BETA_JOB]
    assert len(dialogs) == 3, dialogs
    beta_confirm = dialogs[2]
    assert "Delete 2 finding(s)" in beta_confirm
    assert _MANUAL_TITLE not in beta_confirm

    # Issue #402's Verification asks for the request, confirmation fields, response, DB state and
    # refreshed DOM to be RECORDED -- an assertion that passes silently is not that record. Emitted once
    # here so `pytest -s` (the documented command) leaves the evidence in the log, green or red.
    print("\n[ext#402 evidence] " + json.dumps({
        "preview_request": {"url": preview.url.replace(base, ""), "status": preview.status,
                            "findings": [f["title"] for f in preview_body["findings"]]},
        "destroy_request": {"url": destroy_url.replace(base, ""), "status": destroy_statuses[0]},
        "destroy_linked_job": {"url": beta_destroy_url.replace(base, ""),
                               "status": beta_statuses[0], "confirm": beta_confirm},
        "confirm_text": confirm,
        "dom_findings_before": len(before_dom), "dom_findings_after": len(after_dom),
        "dom_findings_final": len(rendered_finding_ids()),
        "dom_gone": [ids["alpha"][0], ids["alpha"][1], ids["alpha_note"]],
        "dom_kept_after_destroy": [ids["beta"], ids["manual"]],
        "db_rows_before": len(before_db), "db_rows_after": len(after_db),
        "db_titles_after": [t for t, _ in after_db],
        "db_titles_final": [t for t, _ in db_rows()],
        "remove_job_adoption_calls": [call[0] for call in stub.unadopt_calls],
    }, indent=2))


# ----------------------------------------------------- standing-prose editor (ext#346 item 4)


def test_prose_editor_mounts_and_round_trips(page, live_app):
    """The Methodology / Scope standing-prose fields are the kit reporting editor, not bare textareas,
    and a save round-trips through the serialize-to-text + dedup path (scribble.prose):

    1. the two fallback textareas are hidden and a kit editor surface mounts in their place, PREFILLED
       with the generated standing text (ext#346 item 3);
    2. editing the Methodology editor and clicking Save serializes the doc back to the posted
       `methodology_text` field — proven by reloading and finding the override prefilled in the editor.

    This runs LAST in the module (it mutates the demo engagement's methodology override); no other test
    asserts the default methodology text, so the mutation is harmless.
    """
    eng_id = live_app["engagement_id"]
    page.goto(f"{live_app['base_url']}/scribble/engagements/{eng_id}")

    # Open the collapsible prose card so its contents are visible/clickable.
    page.locator(".scribble-report-prose summary").click()

    # Editor mounted over BOTH fields; the real posted textareas are hidden.
    surfaces = page.locator(".scribble-report-prose [data-rp-editor].fr-editor-mounted .fr-editor-surface")
    assert surfaces.count() == 2
    assert page.locator("#rp-methodology").is_visible() is False
    assert page.locator("#rp-scope").is_visible() is False

    # Prefill: the generated standing Methodology text is in the editor, not an empty box.
    methodology = surfaces.first
    assert methodology.inner_text().strip() != ""

    from scribble.models import ReportBoard

    def stored_methodology():
        with live_app["session_factory"]() as db:
            return db.get(ReportBoard, eng_id).methodology_text

    # Dedup interop (ext#346 item 3): saving the PREFILLED default untouched must store NULL, not a
    # duplicate override row echoing the standing text. The editor serializes its doc back to text on
    # submit, and that text flows through prose.override_for_storage -> NULL for the unchanged default.
    with page.expect_navigation():
        page.locator(".scribble-report-prose button[type='submit']").click()
    assert stored_methodology() is None

    # Edit the Methodology editor, then Save (the form submit serializes the doc into the textarea).
    marker = "QA custom methodology round-trip marker."
    page.locator(".scribble-report-prose summary").click()
    methodology = page.locator(
        ".scribble-report-prose [data-rp-editor].fr-editor-mounted .fr-editor-surface"
    ).first
    methodology.click()
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")
    page.keyboard.type(marker)
    with page.expect_navigation():
        page.locator(".scribble-report-prose button[type='submit']").click()

    # A real edit IS persisted (serialize -> store), and reloading prefills the editor from it.
    assert stored_methodology() == marker
    page.goto(f"{live_app['base_url']}/scribble/engagements/{eng_id}")
    page.locator(".scribble-report-prose summary").click()
    reloaded = page.locator(
        ".scribble-report-prose [data-rp-editor].fr-editor-mounted .fr-editor-surface"
    ).first
    assert marker in reloaded.inner_text()
