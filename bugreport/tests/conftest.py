"""Shared fixtures.

Bugreport has no standalone entry point, so the ``app`` fixture mounts it the way a host does: a bare
Flask app, a SQLite engine, ``bugreport.register``. Every host capability arrives through ``cfg.extras``
and is installed here as a *controllable* holder, because the interesting behaviour is not what Bugreport
does with a hook present — it is what it does when a hook is absent, empty, or lying.

Two distinct identities on purpose:

* ``FakeUser`` — the BROWSER-session identity (``bugreport.deps.current_actor()``).
* ``StubActor`` — the ``PatActor``-shaped BEARER identity (``bugreport.host.actor()``). A PAT request
  carries no session at all, so ``pat_client`` blanks ``current_actor``; collapsing the two would hide
  the characteristic bug of a machine route quietly reading the session identity.
"""

from __future__ import annotations

import functools
import uuid
from dataclasses import dataclass, field

import pytest
from flask import Flask, jsonify
from sqlalchemy import create_engine, select

import bugreport
from bugreport.models import Report


class FakeRole:
    def __init__(self, value: str):
        self.value = value

    @property
    def is_admin(self) -> bool:
        return self.value == "admin"


class FakeUser:
    """The BROWSER-session identity."""

    def __init__(self, username: str = "tester", role: str = "operator", ident=None):
        self.id = ident if ident is not None else uuid.uuid7()
        self.username = username
        self.role = FakeRole(role)


@dataclass(frozen=True)
class StubActor:
    """``PatActor``-shaped fake (see lotek ``app/host_contract.py``). ``role`` is the role *value* string
    ("viewer"|"operator"|"admin"), matching the real ``PatActor``."""

    id: uuid.UUID = field(default_factory=uuid.uuid7)
    username: str = "agent"
    role: str = "operator"
    scopes: frozenset[str] = field(default_factory=lambda: frozenset({"read", "write"}))


class AuditLog:
    """Recording fake for the host's audited-write seam. Bugreport calls it INSIDE the same transaction as
    the change (INV-AUDIT-03), so a test can assert the core audit row was written."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def __call__(self, db, action, *, subject_type=None, subject_id=None, before=None, after=None):
        self.events.append({"action": action, "subject_type": subject_type, "subject_id": subject_id,
                            "before": before, "after": after})

    def actions(self) -> list[str]:
        return [e["action"] for e in self.events]


@pytest.fixture
def audit_log():
    return AuditLog()


@pytest.fixture
def hooks(audit_log):
    """Mutable host-capability holder. A test flips one entry and the extension sees it immediately."""
    return {
        "actor": FakeUser(username="alice", role="operator"),
        "can_write": True,
        "audit": audit_log,
        # --- PAT / machine-API hooks (bugreport/api_pat.py) ---
        "pat_actor": StubActor(),
        # None -> authenticated. A (body, status) tuple simulates the host rejecting the token.
        "pat_authenticate": None,
    }


@pytest.fixture
def app(tmp_path, hooks):
    application = Flask(__name__)
    application.config["SECRET_KEY"] = "test"
    engine = create_engine(f"sqlite:///{tmp_path / 'bugreport.db'}", future=True)
    cfg = bugreport.register(
        application, engine, instance_path=str(tmp_path), base_template="bugreport/base.html"
    )
    # A host ALWAYS injects current_actor (even when nobody is logged in, it just returns None). Its mere
    # PRESENCE is what tells deps.is_standalone() we are mounted — see the fail-closed note in deps.py.
    cfg.extras["current_actor"] = lambda: hooks["actor"]
    cfg.extras["can_write"] = lambda: hooks["can_write"]
    cfg.extras["audit"] = hooks["audit"]
    _wire_pat_hooks(cfg, hooks)
    return application


@pytest.fixture
def standalone_app(tmp_path):
    """No host hooks at all — the single-local-user case. `extras` stays empty."""
    application = Flask(__name__)
    application.config["SECRET_KEY"] = "test"
    engine = create_engine(f"sqlite:///{tmp_path / 'standalone.db'}", future=True)
    bugreport.register(
        application, engine, instance_path=str(tmp_path), base_template="bugreport/base.html"
    )
    return application


def _wire_pat_hooks(cfg, hooks) -> None:
    """Install the three PAT capabilities the machine blueprint needs, the way lotek's
    ``app/extensions.py::_inject_host`` installs the real ones.

    ``require_pat_scope`` here REALLY ENFORCES the scope rather than passing through. Whether a token
    *holds* a scope is the host's concern; which scope each route *declares* is Bugreport's, and that is
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
    return app.extensions["bugreport"].session_factory


@pytest.fixture
def pat_client(client, hooks):
    """A test client for the MACHINE surface, shaped the way a real PAT request is: a Bearer principal and
    NO logged-in user. Blanking the session is what turns an accidental ``current_actor`` read on a
    machine route into a visible failure instead of a quiet pass."""
    hooks["actor"] = None
    hooks["can_write"] = False  # the session write gate must be irrelevant on this surface
    return client


def file_report(client, title: str = "it broke", body: str = "details") -> str:
    """File one report through the BROWSER surface and return its id (as a string)."""
    resp = client.post("/bugreport/", data={"title": title, "body": body})
    assert resp.status_code == 302, resp.data
    with client.application.extensions["bugreport"].session_factory() as db:
        row = db.scalars(select(Report).where(Report.title == title)).one()
        return str(row.id)


def load(client, report_id) -> Report | None:
    """Read a row straight out of the database, bypassing every authorization surface — so a test can
    assert what actually happened to it, not what a (possibly buggy) route reported."""
    with client.application.extensions["bugreport"].session_factory() as db:
        return db.get(Report, uuid.UUID(str(report_id)))


def loaded(client, report_id) -> Report:
    """:func:`load`, asserting the row survived — for the cases where a *missing* row is itself the bug
    (a refused write that deleted anyway) and every following assertion would otherwise be skipped."""
    row = load(client, report_id)
    assert row is not None, f"report {report_id} is gone"
    return row
