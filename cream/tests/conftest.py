"""Shared fixtures.

CREAM has no standalone entry point, so the app fixture mounts it the way a host does: a bare Flask app,
a SQLite engine, ``cream.register``. Every host capability hook is installed as a *controllable* holder,
because the interesting behaviour is not what CREAM does with a hook present — it is what it does when a
hook is absent, empty, or throwing, and those three cases are the ones a real host produces.
"""

from __future__ import annotations

import uuid

import pytest
from flask import Flask
from sqlalchemy import create_engine, event

import cream
from cream.seed import seed_defaults


class FakeRole:
    def __init__(self, value: str):
        self.value = value

    @property
    def is_admin(self) -> bool:
        return self.value == "admin"


class FakeUser:
    def __init__(self, username: str = "tester", role: str = "operator"):
        self.id = uuid.uuid7()
        self.username = username
        self.role = FakeRole(role)


@pytest.fixture
def hooks():
    """Mutable host-capability holder. A test flips one entry and the extension sees it immediately."""
    return {
        # An admin by default: most tests are about document behaviour, and branding (the one admin-only
        # surface) would otherwise 403 in every one of them. Tests that care about a *non*-admin set it.
        "actor": FakeUser(role="admin"),
        "can_write": True,
        "can_operate_on": None,        # None -> allow everything
        "visible_engagement_ids": None,  # None -> standalone, no read scoping
        "engagement_scope": None,
        "engagement_units": None,
        "engagement_burn": None,
    }


@pytest.fixture
def app(tmp_path, hooks):
    application = Flask(__name__)
    application.config["SECRET_KEY"] = "test"
    engine = create_engine(f"sqlite:///{tmp_path / 'cream.db'}", future=True)

    # Enforce foreign keys in tests too (SQLite defaults them OFF) so the cascade guards are real.
    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    cfg = cream.register(application, engine, instance_path=str(tmp_path),
                         base_template="cream/base.html")
    cfg.extras["current_actor"] = lambda: hooks["actor"]
    cfg.extras["can_write"] = lambda: hooks["can_write"]
    cfg.extras["can_operate_on"] = lambda eid: (
        True if hooks["can_operate_on"] is None else hooks["can_operate_on"](eid)
    )
    cfg.extras["visible_engagement_ids"] = lambda: hooks["visible_engagement_ids"]
    cfg.extras["engagement_scope"] = lambda eid: (
        [] if hooks["engagement_scope"] is None else hooks["engagement_scope"](eid)
    )
    cfg.extras["engagement_units"] = lambda eid: (
        [] if hooks["engagement_units"] is None else hooks["engagement_units"](eid)
    )
    cfg.extras["engagement_burn"] = lambda eid: (
        {} if hooks["engagement_burn"] is None else hooks["engagement_burn"](eid)
    )
    with cfg.session_factory() as session:
        seed_defaults(session)
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def session_factory(app):
    return app.extensions["cream"].session_factory


@pytest.fixture
def engagement_id():
    return uuid.uuid7()


@pytest.fixture
def make_doc(client, engagement_id):
    """Create a draft through the real API and return its JSON. Tests exercise the surface a caller
    actually has, not a hand-built ORM object that never went through validation."""

    def _make(**body):
        payload = {"engagement_id": str(engagement_id), "title": "Test engagement"}
        payload.update(body)
        res = client.post("/cream/api/documents", json=payload)
        assert res.status_code == 201, res.get_data(as_text=True)
        return res.get_json()

    return _make
