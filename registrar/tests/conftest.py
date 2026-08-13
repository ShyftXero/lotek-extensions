"""Shared fixtures.

Registrar has no standalone entry point, so the ``app`` fixture mounts it the way a host does: a bare
Flask app, a SQLite engine, ``registrar.register``. Every host capability arrives through ``cfg.extras``
and is installed here as a *controllable* holder, because the interesting behaviour is not what Registrar
does with a hook present — it is what it does when a hook is absent, empty, or lying, and those are the
states a real host produces.

The PAT capabilities (``pat_authenticate`` / ``pat_actor`` / ``require_pat_scope``) are what the machine
blueprint (``registrar/api_pat.py``) runs on. The real host injects them AFTER ``register()`` returns, so
this fixture does too.
"""

from __future__ import annotations

import functools
import uuid
from dataclasses import dataclass, field

import pytest
from flask import Flask, jsonify
from sqlalchemy import create_engine, event

import registrar


class FakeRole:
    def __init__(self, value: str):
        self.value = value

    @property
    def is_admin(self) -> bool:
        return self.value == "admin"


class FakeUser:
    """The BROWSER-session identity (``registrar.deps.current_actor()``)."""

    def __init__(self, username: str = "tester", role: str = "operator"):
        self.id = uuid.uuid7()
        self.username = username
        self.role = FakeRole(role)


@dataclass(frozen=True)
class StubActor:
    """``PatActor``-shaped fake (see lotek ``app/host_contract.py``) — the BEARER-token identity, kept
    deliberately distinct from ``FakeUser``. A PAT request carries no session at all, so a fixture that
    collapsed the two would hide this surface's characteristic bug: reading ``current_actor`` (None here)
    and staging an action with a NULL initiator, which is unattributable in the approval audit.

    ``role`` is the role *value* string ("viewer"|"operator"|"admin"), matching the real ``PatActor``.
    """

    id: uuid.UUID = field(default_factory=uuid.uuid7)
    username: str = "agent"
    role: str = "operator"
    scopes: frozenset[str] = field(default_factory=lambda: frozenset({"read", "write"}))


class AuditLog:
    """Recording fake for the host's audited-write seam. Registrar calls it INSIDE the same transaction as
    the change (INV-AUDIT-03), so a test can assert the core audit row was written, not just the local one.
    """

    def __init__(self) -> None:
        self.events: list[dict] = []

    def __call__(self, db, action, *, subject_type=None, subject_id=None, before=None, after=None):
        self.events.append({"action": action, "subject_type": subject_type,
                            "subject_id": subject_id, "before": before, "after": after})

    def actions(self) -> list[str]:
        return [e["action"] for e in self.events]


def _maybe_call(value):
    """Accept either a plain value or a zero-arg callable for the value-shaped hooks, so a test that
    writes ``hooks["visible_engagement_ids"] = lambda: {...}`` scopes what it meant to scope instead of
    silently handing Registrar a function object."""
    return value() if callable(value) else value


@pytest.fixture
def audit_log():
    return AuditLog()


@pytest.fixture
def hooks(audit_log):
    """Mutable host-capability holder. A test flips one entry and the extension sees it immediately."""
    return {
        "actor": FakeUser(role="admin"),
        "can_write": True,
        "can_operate_on": None,          # None -> allow everything
        "visible_engagement_ids": None,  # None -> standalone, no read scoping
        # A browser session is interactive; a PAT never is. `pat_client` flips this to False, which is
        # what makes the INV-EXT-02 approval guard meaningful rather than decorative.
        "is_interactive": True,
        "audit": audit_log,
        # --- PAT / machine-API hooks (registrar/api_pat.py) ---
        "pat_actor": StubActor(),
        # None -> authenticated. A (body, status) tuple simulates the host rejecting the token.
        "pat_authenticate": None,
    }


@pytest.fixture
def app(tmp_path, hooks):
    application = Flask(__name__)
    application.config["SECRET_KEY"] = "test"
    engine = create_engine(f"sqlite:///{tmp_path / 'registrar.db'}", future=True)

    # Enforce foreign keys in tests too (SQLite defaults them OFF) so the cascade guards are real.
    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    cfg = registrar.register(application, engine, instance_path=str(tmp_path),
                             base_template="registrar/base.html")
    cfg.extras["current_actor"] = lambda: hooks["actor"]
    cfg.extras["can_write"] = lambda: hooks["can_write"]
    cfg.extras["can_operate_on"] = lambda eid: (
        True if hooks["can_operate_on"] is None else hooks["can_operate_on"](eid)
    )
    cfg.extras["visible_engagement_ids"] = lambda: _maybe_call(hooks["visible_engagement_ids"])
    cfg.extras["is_interactive"] = lambda: _maybe_call(hooks["is_interactive"])
    cfg.extras["audit"] = hooks["audit"]
    _wire_pat_hooks(cfg, hooks)
    return application


def _wire_pat_hooks(cfg, hooks) -> None:
    """Install the three PAT capabilities the machine blueprint needs, the way lotek's
    ``app/extensions.py::_inject_host`` installs the real ones.

    ``require_pat_scope`` here REALLY ENFORCES the scope rather than passing through. Whether a token
    *holds* a scope is the host's concern; which scope each route *declares* is Registrar's, and that is
    only provable if a read-only token is actually refused by a write route — against a no-op stub every
    route looks correctly gated even with its decorator missing.
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
def client(app):
    return app.test_client()


@pytest.fixture
def session_factory(app):
    return app.extensions["registrar"].session_factory


@pytest.fixture
def pat_client(client, hooks):
    """A test client for the MACHINE surface, with the request shaped the way a real PAT request is: a
    Bearer principal, NO logged-in user, and NOT interactive. Blanking the session is what turns an
    accidental ``current_actor`` read into a visible failure (a NULL initiator) instead of a quiet pass."""
    hooks["actor"] = None
    hooks["can_write"] = False    # the session write gate must be irrelevant on this surface
    hooks["is_interactive"] = False  # a token is never an interactive dashboard session
    return client
