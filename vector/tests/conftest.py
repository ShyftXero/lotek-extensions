"""Shared fixtures.

The app fixture installs a *controllable* host-actor hook into the mounted config's ``extras`` so tests
can simulate different logged-in host users (owner scoping / IDOR) without a real host. When no actor is
set the extension still has a current_actor hook present, so it is treated as a non-admin anonymous user
(sees only builtin + null-owner rows) — matching the mounted posture, not the standalone single-user one.

The PAT capabilities (``pat_authenticate`` / ``pat_actor`` / ``require_pat_scope``) are what the machine
blueprint (``vector/api_pat.py``) runs on. A real host injects them AFTER ``register()`` returns, so the
fixture does too, and hangs a mutable ``app.pat`` holder off the app alongside ``app.holder`` so a test
can swap the token principal between requests.
"""

from __future__ import annotations

import functools
import os
import tempfile
from dataclasses import dataclass, field

import pytest
from flask import jsonify


class FakeRole:
    def __init__(self, value: str):
        self.value = value

    @property
    def is_admin(self) -> bool:
        return self.value == "admin"


class FakeUser:
    def __init__(self, uid: int, username: str, role: str = "operator"):
        self.id = uid
        self.username = username
        self.role = FakeRole(role)
        self.can_write = role in ("operator", "admin")


@dataclass(frozen=True)
class StubActor:
    """``PatActor``-shaped fake (see lotek ``app/host_contract.py``) — the BEARER-token identity, kept
    deliberately distinct from ``FakeUser`` (the browser-session identity). A PAT request carries no
    session at all, so a fixture that collapsed the two would hide this surface's characteristic bug:
    reading ``current_actor`` (None here) and writing a NULL-owner diagram that its own creator then
    cannot see.

    ``role`` is the role *value* string ("viewer"|"operator"|"admin"), matching the real ``PatActor``.
    ``id`` is an int because ``vector.models.Diagram.owner_id`` is still an Integer column — see
    ``api_pat._actor_owner_id``.
    """

    id: int = 7
    username: str = "agent"
    role: str = "operator"
    scopes: frozenset[str] = field(default_factory=lambda: frozenset({"read", "write"}))


def _wire_pat_hooks(cfg, pat) -> None:
    """Install the three PAT capabilities the machine blueprint needs, the way lotek's
    ``app/extensions.py::_inject_host`` installs the real ones.

    ``require_pat_scope`` here REALLY ENFORCES the scope rather than passing through. Whether a token
    *holds* a scope is the host's concern; which scope each route *declares* is Vector's, and that is only
    provable if a read-only token is actually refused by a write route — against a no-op stub every route
    looks correctly gated even with its decorator missing.
    """

    def require_pat_scope(scope: str):
        def decorator(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                actor = pat["actor"]
                if actor is None:
                    return jsonify({"error": "unauthorized"}), 401
                if scope not in actor.scopes:
                    return jsonify({"error": "forbidden", "detail": f"token lacks {scope} scope"}), 403
                return fn(*args, **kwargs)

            return wrapper

        return decorator

    cfg.extras["require_pat_scope"] = require_pat_scope
    cfg.extras["pat_authenticate"] = lambda: pat["authenticate"]  # None -> continue
    cfg.extras["pat_actor"] = lambda: pat["actor"]


@pytest.fixture
def make_app():
    """Factory: build a fresh app (its own temp DB) with a controllable actor holder."""
    created = []

    def _make(seed: bool = False):
        from vector.standalone import create_app

        d = tempfile.mkdtemp()
        app = create_app(db_path=os.path.join(d, "t.sqlite"), instance_path=d, testing=True, seed=seed)
        holder = {"actor": None}
        cfg = app.extensions["vector"]
        cfg.extras["current_actor"] = lambda: holder["actor"]
        cfg.extras["can_write"] = lambda: (
            holder["actor"] is None or bool(getattr(holder["actor"], "can_write", True))
        )
        pat = {"actor": StubActor(), "authenticate": None}
        _wire_pat_hooks(cfg, pat)
        app.holder = holder  # type: ignore[attr-defined]
        app.pat = pat  # type: ignore[attr-defined]
        created.append(app)
        return app

    return _make


@pytest.fixture
def app(make_app):
    return make_app(seed=False)


@pytest.fixture
def client(app):
    return app.test_client()


def login(app, user: FakeUser | None):
    app.holder["actor"] = user


@pytest.fixture
def pat_client(app):
    """A test client for the MACHINE surface, with the request shaped the way a real PAT request is: a
    Bearer principal and NO logged-in session user. Blanking the session is what turns an accidental
    ``current_actor`` read into a visible failure (a diagram its own creator cannot see) rather than a
    quiet pass."""
    login(app, None)
    return app.test_client()
