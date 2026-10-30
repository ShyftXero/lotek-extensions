"""ext#321 — the scribble ``finding`` kind for the unified autosave seam.

``scribble.autosave_api`` now exposes three one-home functions — ``save_finding_block`` /
``load_finding_block`` / ``authorize_finding`` — that BOTH the legacy ``/findings/<id>/blocks`` route AND
the unified-autosave ``finding`` kind call, so a finding block has a single writer (the drift this seam
kills). The authz gate is mandatory and fail-closed; this pins it.

Two layers:
  * ``TestOneHome`` — the extracted functions directly, in scribble's own env (no kit needed). This is the
    INV-TENANCY check for the finding surface: authorize is True only for a logged-in actor on an existing
    finding, and save/load round-trip + report absence as None.
  * ``TestSeam`` — the SAME functions driven THROUGH the real ``/_kit/autosave`` endpoint + the register()
    wiring. Needs ``lotek_kit`` installed alongside scribble (as in core/prod); skipped in a bare scribble
    checkout.
"""
from __future__ import annotations

import types
import uuid

import pytest
from flask import Flask
from sqlalchemy import create_engine

import scribble
from scribble import autosave_api
from scribble.models import BoardFinding, ReportBoard
from scribble.seed import seed_defaults

SAMPLE_DOC = {
    "type": "doc",
    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Finding body."}]}],
}


class _Actor:
    """Stand-in for the host's ``g.current_user`` — any non-None object is a logged-in actor."""

    id = "u1"
    username = "operator"


@pytest.fixture
def app(tmp_path):
    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = "test"
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    cfg = scribble.register(
        flask_app, engine, instance_path=str(tmp_path), base_template="scribble/base.html"
    )
    with cfg.session_factory() as session:
        seed_defaults(session)
        session.commit()
    return flask_app


@pytest.fixture
def session_factory(app):
    return app.extensions["scribble"].session_factory


@pytest.fixture
def finding_id(session_factory) -> str:
    with session_factory() as db:
        engagement = ReportBoard(name="Seam Test", company_name="Acme")
        finding = BoardFinding(engagement=engagement, title="XSS", content_json={}, content_html={})
        db.add(engagement)
        db.add(finding)
        db.commit()
        return str(finding.id)


# ── the one-home functions, directly (the new logic; no kit dependency) ────────────────────────────


class TestOneHome:
    def test_save_then_load_round_trips(self, app, session_factory, finding_id):
        with app.app_context():
            html = autosave_api.save_finding_block(finding_id, "description", SAMPLE_DOC)
            assert html and "Finding body" in html
            doc, cached = autosave_api.load_finding_block(finding_id, "description")
            assert doc == SAMPLE_DOC and cached == html
        # Really persisted on the row (not just echoed).
        with session_factory() as db:
            assert db.get(BoardFinding, uuid.UUID(finding_id)).content_json["description"] == SAMPLE_DOC

    def test_missing_finding_is_none(self, app):
        with app.app_context():
            assert autosave_api.save_finding_block(str(uuid.uuid4()), "description", SAMPLE_DOC) is None
            assert autosave_api.load_finding_block(str(uuid.uuid4()), "description") is None

    def test_non_uuid_id_is_none(self, app):
        with app.app_context():
            assert autosave_api.save_finding_block("not-a-uuid", "description", SAMPLE_DOC) is None
            assert autosave_api.load_finding_block("not-a-uuid", "description") is None

    def test_authorize_allows_logged_in_actor_on_existing_finding(self, app, finding_id):
        with app.app_context():
            assert autosave_api.authorize_finding(_Actor(), finding_id) is True

    def test_authorize_refuses_anonymous(self, app, finding_id):
        with app.app_context():
            assert autosave_api.authorize_finding(None, finding_id) is False

    def test_authorize_refuses_missing_or_bad_id(self, app):
        with app.app_context():
            assert autosave_api.authorize_finding(_Actor(), str(uuid.uuid4())) is False
            assert autosave_api.authorize_finding(_Actor(), "not-a-uuid") is False


# ── the SAME functions through the real seam endpoint + the register() wiring ──────────────────────


class TestSeam:
    """Needs lotek_kit installed alongside scribble (core/prod); skipped in a bare scribble checkout."""

    SEAM = "/_kit/autosave"
    LEGACY = "/scribble/api/findings"

    @pytest.fixture
    def autosave(self):
        return pytest.importorskip("lotek_kit.autosave")

    @pytest.fixture
    def actor_holder(self):
        return types.SimpleNamespace(actor=_Actor())

    @pytest.fixture
    def seam_app(self, app, autosave, actor_holder):
        # Mount the seam exactly as core does (actor = the host's current user). scribble.register()
        # already wired the finding kind; re-assert so a sibling module that cleared the process-global
        # registry can't make this flaky.
        autosave.ensure_autosave_registered(app, actor=lambda: actor_holder.actor)
        if "finding" not in autosave.registered_kinds():
            autosave.register_autosave_kind(
                "finding",
                load=autosave_api.load_finding_block,
                save=autosave_api.save_finding_block,
                authorize=autosave_api.authorize_finding,
            )
        return app

    @pytest.fixture
    def client(self, seam_app):
        return seam_app.test_client()

    def _save(self, client, kind, cid, block="description", doc=SAMPLE_DOC):
        return client.post(self.SEAM, json={"kind": kind, "id": cid, "block": block, "doc": doc})

    def test_authorized_round_trip_through_the_seam(self, client, finding_id):
        r = self._save(client, "finding", finding_id)
        assert r.status_code == 200 and "Finding body" in r.get_json()["html"]
        r = client.get(self.SEAM, query_string={"kind": "finding", "id": finding_id, "block": "description"})
        assert r.status_code == 200 and r.get_json()["doc"] == SAMPLE_DOC

    def test_anonymous_actor_refused_403_and_never_saves(
        self, client, session_factory, finding_id, actor_holder
    ):
        actor_holder.actor = None
        assert self._save(client, "finding", finding_id).status_code == 403
        with session_factory() as db:
            assert db.get(BoardFinding, uuid.UUID(finding_id)).content_json == {}

    def test_unknown_kind_404(self, client, finding_id):
        assert self._save(client, "nope", finding_id).status_code == 404

    def test_missing_finding_refused(self, client):
        assert self._save(client, "finding", str(uuid.uuid4())).status_code == 403

    def test_seam_and_legacy_are_one_writer(self, client, finding_id):
        # Write via the seam, read through the legacy route — and vice-versa: same bytes either way.
        assert client.post(
            self.SEAM, json={"kind": "finding", "id": finding_id, "block": "remediation", "doc": SAMPLE_DOC}
        ).status_code == 200
        r = client.get(f"{self.LEGACY}/{finding_id}/blocks/remediation")
        assert r.status_code == 200 and r.get_json()["doc"] == SAMPLE_DOC

        assert client.post(f"{self.LEGACY}/{finding_id}/blocks/details", json=SAMPLE_DOC).status_code == 200
        r = client.get(self.SEAM, query_string={"kind": "finding", "id": finding_id, "block": "details"})
        assert r.status_code == 200 and r.get_json()["doc"] == SAMPLE_DOC

    def test_register_wires_exactly_the_finding_kind(self, autosave):
        from flask import Blueprint

        saved_kinds, saved_flag = dict(autosave._KINDS), autosave_api._REGISTERED
        autosave._KINDS.clear()
        autosave_api._REGISTERED = False
        try:
            # Throwaway blueprints so the shared api_bp/bp singletons are never touched.
            autosave_api.register(Blueprint("t_api", __name__), Blueprint("t_ui", __name__))
            assert autosave.registered_kinds() == frozenset({"finding"})
        finally:
            autosave._KINDS.clear()
            autosave._KINDS.update(saved_kinds)
            autosave_api._REGISTERED = saved_flag
