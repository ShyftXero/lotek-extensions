"""Shared fixtures.

CREAM has no standalone entry point, so the app fixture mounts it the way a host does: a bare Flask app,
a SQLite engine, ``cream.register``. Every host capability hook is installed as a *controllable* holder,
because the interesting behaviour is not what CREAM does with a hook present — it is what it does when a
hook is absent, empty, or throwing, and those three cases are the ones a real host produces.
"""

from __future__ import annotations

import functools
import uuid
from dataclasses import dataclass, field

import pytest
from flask import Flask, jsonify
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


@dataclass(frozen=True)
class StubActor:
    """``PatActor``-shaped fake (see lotek ``app/host_contract.py``) for the machine blueprint — the
    BEARER-token identity, deliberately distinct from ``FakeUser`` (the browser-session identity). Real
    lotek keeps the two apart (cookie vs token) and a PAT request has NO session at all, so a test that
    collapsed them would hide the exact bug this surface is prone to: reading ``current_actor`` (None on a
    machine request) and silently writing a NULL ``owner_id``.

    ``role`` is the role *value* string ("viewer"|"operator"|"admin"), matching the real ``PatActor``.
    """

    id: uuid.UUID = field(default_factory=uuid.uuid7)
    username: str = "agent"
    role: str = "operator"
    scopes: frozenset[str] = field(default_factory=lambda: frozenset({"read", "write"}))


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
        # --- PAT / machine-API hooks (cream/api_pat.py) ---
        # The token's principal. `pat_client` blanks "actor" so the request looks like a real machine
        # request (Bearer, no cookie); this stays set, and is the only identity api_pat may attribute to.
        "pat_actor": StubActor(),
        # None -> authenticated. A test sets a (body, status) tuple to simulate the host rejecting the
        # token, which is what the real `pat_authenticate` returns on a bad/expired one.
        "pat_authenticate": None,
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
    _wire_pat_hooks(cfg, hooks)
    with cfg.session_factory() as session:
        seed_defaults(session)
    return application


def _wire_pat_hooks(cfg, hooks) -> None:
    """Install the three PAT capabilities the machine blueprint needs, the way lotek's
    ``app/extensions.py::_inject_host`` installs the real ones (into ``cfg.extras``, AFTER ``register()``).

    ``require_pat_scope`` here REALLY ENFORCES the scope rather than passing through. Scope RBAC is the
    host's concern, but *which scope each route declares* is cream's, and that is only provable if a
    read-only token is actually refused by a write route — a no-op stub would make every route look
    correctly gated even if its decorator were missing or named the wrong scope.
    """

    def require_pat_scope(scope: str):
        def decorator(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                actor = hooks["pat_actor"]
                if actor is None:
                    return jsonify({"error": "unauthorized"}), 401
                if scope not in actor.scopes:
                    return jsonify({"error": "forbidden", "detail": f"token lacks {scope} scope"}), 403
                return fn(*args, **kwargs)

            return wrapper

        return decorator

    def pat_authenticate():
        return hooks["pat_authenticate"]  # None -> continue (Flask before_request success)

    cfg.extras["require_pat_scope"] = require_pat_scope
    cfg.extras["pat_authenticate"] = pat_authenticate
    cfg.extras["pat_actor"] = lambda: hooks["pat_actor"]


@pytest.fixture
def pat_client(client, hooks):
    """A test client for the MACHINE surface. Blanks the session hooks so the request has the shape a real
    PAT request has — a Bearer principal and NO logged-in user — which is what makes an accidental
    ``current_actor`` read show up as a failure (NULL owner) instead of quietly passing."""
    hooks["actor"] = None
    hooks["can_write"] = False  # the session write gate must be irrelevant on this surface
    return client


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
