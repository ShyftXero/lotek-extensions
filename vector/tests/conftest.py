"""Shared fixtures.

The app fixture installs a *controllable* host-actor hook into the mounted config's ``extras`` so tests
can simulate different logged-in host users (owner scoping / IDOR) without a real host. When no actor is
set the extension still has a current_actor hook present, so it is treated as a non-admin anonymous user
(sees only builtin + null-owner rows) — matching the mounted posture, not the standalone single-user one.
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest


class FakeRole:
    def __init__(self, value: str):
        self.value = value

    @property
    def is_admin(self) -> bool:
        return self.value == "admin"


class FakeUser:
    def __init__(self, uid: uuid.UUID, username: str, role: str = "operator"):
        self.id = uid
        self.username = username
        self.role = FakeRole(role)
        self.can_write = role in ("operator", "admin")


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
        app.holder = holder  # type: ignore[attr-defined]
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
