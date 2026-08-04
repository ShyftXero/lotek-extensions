"""Tests for WS4 Phase A: block autosave (scribble/autosave_api.py) + presence
(scribble/collab/presence.py).

These modules don't touch scribble/api.py, scribble/blueprint.py, or scribble/__init__.py (frozen for
this workstream — see plans/CONTRACTS.md ownership map); instead they expose ``register(api_bp, bp)``
hooks meant to be wired into the mount path by the driver. Here we drive them the same way: import the
shared blueprint singletons and call our own ``register()`` before ``scribble.register()`` runs
``app.register_blueprint(...)``.
"""

from __future__ import annotations

import pytest
from flask import Flask
from sqlalchemy import create_engine

import scribble
import scribble.collab.presence as presence_module
from scribble import autosave_api
from scribble.api import api_bp
from scribble.blueprint import bp
from scribble.content import schema
from scribble.models import Engagement, EngagementFinding
from scribble.seed import seed_defaults

API_PREFIX = "/scribble/api"

SAMPLE_DOC = {
    "type": "doc",
    "content": [
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "Affected host: "},
                {"type": "variable", "attrs": {"key": "TARGET_HOST"}},
            ],
        }
    ],
}


def _url(finding_id: int, block: str, suffix: str = "") -> str:
    return f"{API_PREFIX}/findings/{finding_id}/blocks/{block}{suffix}"


@pytest.fixture(autouse=True)
def _isolated_presence(monkeypatch):
    """The presence registry is a module-level singleton; swap in a fresh one per test so heartbeats
    from one test never leak into another."""
    fresh = presence_module.PresenceRegistry()
    monkeypatch.setattr(presence_module, "registry", fresh)
    yield fresh


@pytest.fixture
def app(tmp_path):
    # Wire WS4's routes onto the shared blueprint objects exactly as the driver will (before
    # scribble.register() calls app.register_blueprint). Both register() calls are idempotent, so
    # re-running this fixture across tests never double-registers routes on the blueprint singletons.
    autosave_api.register(api_bp, bp)
    presence_module.register(api_bp, bp)

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
def client(app):
    return app.test_client()


@pytest.fixture
def session_factory(app):
    return app.extensions["scribble"].session_factory


@pytest.fixture
def finding_id(session_factory) -> int:
    with session_factory() as db:
        engagement = Engagement(name="Autosave Test", company_name="Acme")
        finding = EngagementFinding(engagement=engagement, title="XSS", content_json={}, content_html={})
        db.add(engagement)
        db.add(finding)
        db.commit()
        return finding.id


# --------------------------------------------------------------------------------------- autosave


def test_autosave_stores_json_and_caches_html(client, session_factory, finding_id):
    resp = client.post(_url(finding_id, "description"), json=SAMPLE_DOC)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert "Affected host" in data["html"]
    # No engagement context is resolved at autosave time; the editor-preview render keeps the raw
    # {{KEY}} chip literal (WS7's report context is what actually resolves variables).
    assert "{{TARGET_HOST}}" in data["html"]

    with session_factory() as db:
        finding = db.get(EngagementFinding, finding_id)
        assert finding.content_json["description"] == SAMPLE_DOC
        assert "Affected host" in finding.content_html["description"]


def test_autosave_only_touches_target_block(client, session_factory, finding_id):
    client.post(_url(finding_id, "description"), json=SAMPLE_DOC)
    other_doc = schema.doc_from_text("Remediate by patching.")
    client.post(_url(finding_id, "remediation"), json=other_doc)

    with session_factory() as db:
        finding = db.get(EngagementFinding, finding_id)
        assert finding.content_json["description"] == SAMPLE_DOC
        assert finding.content_json["remediation"] == other_doc
        assert "Remediate" in finding.content_html["remediation"]


def test_get_round_trips(client, finding_id):
    assert client.post(_url(finding_id, "description"), json=SAMPLE_DOC).status_code == 200

    resp = client.get(_url(finding_id, "description"))
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["doc"] == SAMPLE_DOC
    assert "Affected host" in data["html"]


def test_get_unwritten_block_returns_empty_doc(client, finding_id):
    resp = client.get(_url(finding_id, "remediation"))
    assert resp.status_code == 200
    data = resp.get_json()
    assert schema.is_doc(data["doc"])
    assert data["doc"]["content"] == []
    assert data["html"] == ""


@pytest.mark.parametrize(
    "body",
    [
        {"type": "paragraph", "content": []},  # a node, but not a doc
        {"content": []},  # missing "type"
        ["not", "an", "object"],
        "just a string",
        None,
        123,
    ],
)
def test_malformed_body_rejected(client, finding_id, body):
    resp = client.post(_url(finding_id, "description"), json=body)
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert "error" in data


def test_bare_doc_with_no_content_key_is_accepted(client, finding_id):
    """{'type': 'doc'} (content omitted) is still a valid, if minimal, doc — not malformed."""
    resp = client.post(_url(finding_id, "description"), json={"type": "doc"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_autosave_missing_finding_404(client):
    resp = client.post(_url(999999, "description"), json=SAMPLE_DOC)
    assert resp.status_code == 404


def test_get_missing_finding_404(client):
    resp = client.get(_url(999999, "description"))
    assert resp.status_code == 404


# --------------------------------------------------------------------------------------- presence


def test_presence_starts_empty(client, finding_id):
    resp = client.get(_url(finding_id, "description", "/presence"))
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["editors"] == []
    assert data["count"] == 0


def test_presence_heartbeat_then_read(client, finding_id):
    beat = client.post(_url(finding_id, "description", "/presence"), json={"user": "alice"})
    assert beat.status_code == 200
    data = beat.get_json()
    assert data["ok"] is True
    assert data["count"] == 1
    assert data["editors"][0]["user"] == "alice"
    assert "seconds_ago" in data["editors"][0]

    client.post(_url(finding_id, "description", "/presence"), json={"user": "bob"})

    listing = client.get(_url(finding_id, "description", "/presence"))
    users = {e["user"] for e in listing.get_json()["editors"]}
    assert users == {"alice", "bob"}


def test_presence_defaults_to_anonymous(client, finding_id):
    resp = client.post(_url(finding_id, "description", "/presence"), json={})
    assert resp.get_json()["editors"][0]["user"] == "anonymous"


def test_presence_leave_removes_user(client, finding_id):
    client.post(_url(finding_id, "description", "/presence"), json={"user": "alice"})
    resp = client.post(_url(finding_id, "description", "/presence"), json={"user": "alice", "leave": True})
    assert resp.get_json()["editors"] == []


def test_presence_is_scoped_per_block(client, finding_id):
    client.post(_url(finding_id, "description", "/presence"), json={"user": "alice"})
    other_block = client.get(_url(finding_id, "remediation", "/presence"))
    assert other_block.get_json()["editors"] == []

    same_block = client.get(_url(finding_id, "description", "/presence"))
    assert len(same_block.get_json()["editors"]) == 1


def test_presence_registry_ttl_expiry():
    registry = presence_module.PresenceRegistry(ttl_seconds=5)
    registry.heartbeat(1, "description", "alice", now=1000.0)
    assert [e["user"] for e in registry.active(1, "description", now=1002.0)] == ["alice"]
    assert registry.active(1, "description", now=1010.0) == []
