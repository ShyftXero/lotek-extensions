"""AI "Rephrase with AI" route (scribble/draft_api.py) — Phase 3 of the lotek AI engine.

Drives the route the same way test_editor.py drives autosave: register the module's routes onto the
shared blueprint singletons before scribble.register() runs app.register_blueprint. A fake `ai_stream`
is injected into the host `extras` (exactly how core's app.ai.stream is exposed at runtime); no live
LLM is contacted. The load-bearing property is fail-closed: with no `ai_stream` hook the route returns
503 inline text, never a 500 and never silence.
"""
from __future__ import annotations

import uuid

import pytest
from flask import Flask
from sqlalchemy import create_engine

import scribble
from scribble import draft_api
from scribble.api import api_bp
from scribble.blueprint import bp
from scribble.content import schema
from scribble.models import Engagement, EngagementFinding
from scribble.seed import seed_defaults

API_PREFIX = "/scribble/api"


def _url(finding_id, block: str) -> str:
    return f"{API_PREFIX}/findings/{finding_id}/rephrase/{block}"


@pytest.fixture
def app(tmp_path):
    draft_api.register(api_bp, bp)  # idempotent, mirrors test_editor.py's wiring
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
def finding_id(session_factory):
    with session_factory() as db:
        eng = Engagement(name="Draft Test", company_name="Acme")
        finding = EngagementFinding(engagement=eng, title="Reflected XSS", content_json={}, content_html={})
        db.add(eng)
        db.add(finding)
        db.commit()
        return finding.id


def _set_ai_stream(app, fn):
    app.extensions["scribble"].extras["ai_stream"] = fn


def test_rephrase_streams_reply(app, client, finding_id):
    seen: dict = {}

    def fake_stream(messages, **kwargs):
        seen["messages"] = messages
        return iter(["Refl", "ected ", "XSS."])

    _set_ai_stream(app, fake_stream)
    resp = client.post(_url(finding_id, "description"))
    assert resp.status_code == 200
    assert resp.get_data(as_text=True) == "Reflected XSS."
    joined = " ".join(m["content"] for m in seen["messages"])
    assert "Reflected XSS" in joined and "description" in joined  # finding title + section in prompt


def test_empty_block_drafts_then_text_block_rephrases(app, client, session_factory, finding_id):
    seen: dict = {}

    def fake_stream(messages, **kwargs):
        seen["messages"] = messages
        return iter(["ok"])

    _set_ai_stream(app, fake_stream)

    # empty block -> DRAFT framing (no "Rephrase this section")
    client.post(_url(finding_id, "description"))
    assert "Rephrase this section" not in seen["messages"][-1]["content"]

    # give the block text -> REPHRASE framing carries the existing prose
    with session_factory() as db:
        f = db.get(EngagementFinding, finding_id)
        f.content_json = {"description": schema.doc_from_text("teh servr is vulnrable")}
        db.commit()
    client.post(_url(finding_id, "description"))
    user = seen["messages"][-1]["content"]
    assert "Rephrase this section" in user and "teh servr is vulnrable" in user


def test_fails_closed_when_no_ai_hook(app, client, finding_id):
    app.extensions["scribble"].extras.pop("ai_stream", None)
    resp = client.post(_url(finding_id, "description"))
    assert resp.status_code == 503
    assert b"unavailable" in resp.data.lower()


def test_unknown_finding_404(app, client):
    _set_ai_stream(app, lambda messages, **k: iter(["x"]))
    resp = client.post(_url(uuid.uuid4(), "description"))
    assert resp.status_code == 404


def test_hook_raising_degrades_to_503_not_500(app, client, finding_id):
    def boom(messages, **kwargs):
        raise RuntimeError("AiUnavailable: AI completion is disabled")

    _set_ai_stream(app, boom)
    resp = client.post(_url(finding_id, "description"))
    assert resp.status_code == 503
    assert b"AI unavailable" in resp.data
