"""ext#321 — the scribble ``finding`` kind for the unified autosave seam.

``scribble.autosave_api`` now exposes three one-home functions — ``save_finding_block`` /
``load_finding_block`` / ``authorize_finding`` — that BOTH the legacy ``/findings/<id>/blocks`` route AND
the unified-autosave ``finding`` kind call, so a finding block has a single writer (the drift this seam
kills).

The legacy route is protected by scribble's blueprint-wide ``before_request`` tenancy gate
(``authz._gate`` → ``can_view_engagement``). The seam is on CORE's own blueprint with the id in the BODY,
so that gate can't reach it — the kind's ``authorize`` is the ONLY tenancy gate, so it MUST re-run
``can_view_engagement`` (mirroring ``artifacts_api.create_artifact``). These tests pin that, including the
INV-TENANCY make-or-break case: a foreign-client actor is refused.

Two layers:
  * ``TestOneHome`` — the extracted functions directly (no kit needed): round-trip + the authz gate,
    foreign-tenant included.
  * ``TestSeam`` — the SAME functions through the real ``/_kit/autosave`` endpoint + the register() wiring,
    ``importorskip``'d on ``lotek_kit`` (runs in core/prod, skipped in a bare scribble checkout).
"""
from __future__ import annotations

import uuid

import pytest

from scribble import autosave_api
from scribble.models import BoardFinding, ReportBoard

# conftest.py provides: app, session_factory, stub_host (+ StubUser/_StubRole for the actor shapes).
from tests.conftest import StubUser, _StubRole

SAMPLE_DOC = {
    "type": "doc",
    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Finding body."}]}],
}

OWN_CLIENT = 555
FOREIGN_CLIENT = 777


def _make_finding(session_factory, client_id: int) -> str:
    with session_factory() as db:
        engagement = ReportBoard(name="Seam Test", company_name="Acme", client_id=client_id)
        finding = BoardFinding(engagement=engagement, title="XSS", content_json={}, content_html={})
        db.add(engagement)
        db.add(finding)
        db.commit()
        return str(finding.id)


# ── the one-home functions, directly ───────────────────────────────────────────────────────────────


class TestOneHome:
    def test_save_then_load_round_trips(self, app, session_factory):
        fid = _make_finding(session_factory, OWN_CLIENT)
        with app.app_context():
            html = autosave_api.save_finding_block(fid, "description", SAMPLE_DOC)
            assert html and "Finding body" in html
            doc, cached = autosave_api.load_finding_block(fid, "description")
            assert doc == SAMPLE_DOC and cached == html
        with session_factory() as db:
            assert db.get(BoardFinding, uuid.UUID(fid)).content_json["description"] == SAMPLE_DOC

    def test_missing_finding_is_none(self, app):
        with app.app_context():
            assert autosave_api.save_finding_block(str(uuid.uuid4()), "description", SAMPLE_DOC) is None
            assert autosave_api.load_finding_block(str(uuid.uuid4()), "description") is None

    def test_non_uuid_id_is_none(self, app):
        with app.app_context():
            assert autosave_api.save_finding_block("not-a-uuid", "description", SAMPLE_DOC) is None
            assert autosave_api.load_finding_block("not-a-uuid", "description") is None

    def test_authorize_allows_actor_who_can_view_the_client(self, app, session_factory, stub_host):
        # Default stub actor is an admin → can_view_client True for any client.
        fid = _make_finding(session_factory, FOREIGN_CLIENT)
        with app.app_context():
            assert autosave_api.authorize_finding(stub_host.current_user, fid) is True

    def test_authorize_refuses_foreign_tenant(self, app, session_factory, stub_host):
        # The INV-TENANCY make-or-break: a non-admin actor who can see OWN_CLIENT but not FOREIGN_CLIENT.
        stub_host.current_user = StubUser(id=2, username="bob", role=_StubRole("operator"))
        stub_host.viewable_client_ids = {OWN_CLIENT}
        own = _make_finding(session_factory, OWN_CLIENT)
        foreign = _make_finding(session_factory, FOREIGN_CLIENT)
        with app.app_context():
            assert autosave_api.authorize_finding(stub_host.current_user, own) is True
            assert autosave_api.authorize_finding(stub_host.current_user, foreign) is False

    def test_authorize_refuses_anonymous(self, app, session_factory, stub_host):
        stub_host.current_user = None
        fid = _make_finding(session_factory, OWN_CLIENT)
        with app.app_context():
            assert autosave_api.authorize_finding(None, fid) is False

    def test_authorize_refuses_missing_or_bad_id(self, app, stub_host):
        with app.app_context():
            assert autosave_api.authorize_finding(stub_host.current_user, str(uuid.uuid4())) is False
            assert autosave_api.authorize_finding(stub_host.current_user, "not-a-uuid") is False


# ── the SAME functions through the real seam endpoint + the register() wiring ──────────────────────


class TestSeam:
    """Needs lotek_kit installed alongside scribble (core/prod); skipped in a bare scribble checkout."""

    SEAM = "/_kit/autosave"
    LEGACY = "/scribble/api/findings"

    @pytest.fixture
    def autosave(self):
        return pytest.importorskip("lotek_kit.autosave")

    @pytest.fixture
    def client(self, app, autosave, stub_host):
        # Mount the seam exactly as core does: the actor is the host's current user (the stub we control).
        autosave.ensure_autosave_registered(app, actor=lambda: stub_host.current_user)
        if "finding" not in autosave.registered_kinds():
            autosave.register_autosave_kind(
                "finding",
                load=autosave_api.load_finding_block,
                save=autosave_api.save_finding_block,
                authorize=autosave_api.authorize_finding,
            )
        return app.test_client()

    def _save(self, client, kind, cid, block="description", doc=SAMPLE_DOC):
        return client.post(self.SEAM, json={"kind": kind, "id": cid, "block": block, "doc": doc})

    def test_authorized_round_trip_through_the_seam(self, client, session_factory):
        fid = _make_finding(session_factory, OWN_CLIENT)  # admin actor → authorized
        r = self._save(client, "finding", fid)
        assert r.status_code == 200 and "Finding body" in r.get_json()["html"]
        r = client.get(self.SEAM, query_string={"kind": "finding", "id": fid, "block": "description"})
        assert r.status_code == 200 and r.get_json()["doc"] == SAMPLE_DOC

    def test_foreign_tenant_refused_403_own_client_200(self, client, session_factory, stub_host):
        # INV-TENANCY through the real endpoint: the gap a cross-blueprint seam reopens.
        stub_host.current_user = StubUser(id=2, username="bob", role=_StubRole("operator"))
        stub_host.viewable_client_ids = {OWN_CLIENT}
        own = _make_finding(session_factory, OWN_CLIENT)
        foreign = _make_finding(session_factory, FOREIGN_CLIENT)
        assert self._save(client, "finding", own).status_code == 200
        assert self._save(client, "finding", foreign).status_code == 403
        # the refused write never reached save
        with session_factory() as db:
            assert db.get(BoardFinding, uuid.UUID(foreign)).content_json == {}

    def test_anonymous_actor_refused_403(self, client, session_factory, stub_host):
        stub_host.current_user = None
        fid = _make_finding(session_factory, OWN_CLIENT)
        assert self._save(client, "finding", fid).status_code == 403

    def test_unknown_kind_404(self, client, session_factory):
        fid = _make_finding(session_factory, OWN_CLIENT)
        assert self._save(client, "nope", fid).status_code == 404

    def test_missing_finding_refused(self, client):
        assert self._save(client, "finding", str(uuid.uuid4())).status_code == 403

    def test_seam_and_legacy_are_one_writer(self, client, session_factory):
        fid = _make_finding(session_factory, OWN_CLIENT)  # admin actor passes both gates
        # Write via the seam, read through the legacy route — and vice-versa: same bytes either way.
        assert self._save(client, "finding", fid, block="remediation").status_code == 200
        r = client.get(f"{self.LEGACY}/{fid}/blocks/remediation")
        assert r.status_code == 200 and r.get_json()["doc"] == SAMPLE_DOC

        assert client.post(f"{self.LEGACY}/{fid}/blocks/details", json=SAMPLE_DOC).status_code == 200
        r = client.get(self.SEAM, query_string={"kind": "finding", "id": fid, "block": "details"})
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
