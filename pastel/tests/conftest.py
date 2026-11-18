"""Shared test fixtures for PASTEL.

Mounts the extension the way a host does: bare Flask app + SQLite + pastel.register.
Capabilities arrive through cfg.extras as controllable fakes. PAT machine-API tests blank
the session identity so a machine route accidentally reading the session is a visible failure.
"""

from __future__ import annotations

import functools
import uuid
from dataclasses import dataclass, field

import pytest
from flask import Flask, jsonify
from sqlalchemy import create_engine

import pastel


class FakeRole:
    def __init__(self, value: str):
        self.value = value

    @property
    def is_admin(self) -> bool:
        return self.value == "admin"


class FakeUser:
    def __init__(self, username: str = "tester", role: str = "operator", ident=None):
        self.id = ident if ident is not None else uuid.uuid7()
        self.username = username
        self.role = FakeRole(role)


@dataclass(frozen=True)
class StubActor:
    id: uuid.UUID = field(default_factory=uuid.uuid7)
    username: str = "agent"
    role: str = "operator"
    scopes: frozenset[str] = field(default_factory=lambda: frozenset({"read", "write"}))


class AuditLog:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def __call__(self, db, action, *, subject_type=None, subject_id=None, before=None, after=None):
        self.events.append({"action": action, "subject_type": subject_type,
                            "subject_id": subject_id, "before": before, "after": after})

    def actions(self) -> list[str]:
        return [e["action"] for e in self.events]


@pytest.fixture
def audit_log():
    return AuditLog()


@pytest.fixture
def hooks(audit_log):
    return {
        "actor": FakeUser(username="alice", role="operator"),
        "can_write": True,
        "audit": audit_log,
        "pat_actor": StubActor(),
        "pat_authenticate": None,
    }


@pytest.fixture
def app(tmp_path, hooks):
    application = Flask(__name__)
    application.config["SECRET_KEY"] = "test"
    engine = create_engine(f"sqlite:///{tmp_path / 'pastel.db'}", future=True)
    cfg = pastel.register(
        application, engine, instance_path=str(tmp_path), base_template="pastel/base.html"
    )
    cfg.extras["current_actor"] = lambda: hooks["actor"]
    cfg.extras["can_write"] = lambda: hooks["can_write"]
    cfg.extras["audit"] = hooks["audit"]
    _wire_pat_hooks(cfg, hooks)
    return application


@pytest.fixture
def standalone_app(tmp_path):
    application = Flask(__name__)
    application.config["SECRET_KEY"] = "test"
    engine = create_engine(f"sqlite:///{tmp_path / 'standalone.db'}", future=True)
    pastel.register(
        application, engine, instance_path=str(tmp_path), base_template="pastel/base.html"
    )
    return application


def _wire_pat_hooks(cfg, hooks) -> None:
    def require_pat_scope(scope: str):
        def decorator(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                actor = hooks["pat_actor"]
                if actor is None:
                    return jsonify({"error": "unauthorized"}), 401
                if scope not in actor.scopes:
                    return jsonify({"error": "forbidden"}), 403
                return fn(*args, **kwargs)
            return wrapper
        return decorator

    def pat_authenticate():
        return hooks["pat_authenticate"]

    cfg.extras["require_pat_scope"] = require_pat_scope
    cfg.extras["pat_authenticate"] = pat_authenticate
    cfg.extras["pat_actor"] = lambda: hooks["pat_actor"]


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def session_factory(app):
    return app.extensions["pastel"].session_factory


@pytest.fixture
def pat_client(client, hooks):
    """Test client shaped like a PAT request: no session, Bearer principal only."""
    hooks["actor"] = None
    hooks["can_write"] = False
    return client
