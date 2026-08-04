"""Tests for the scribble.deps host-hook accessors added for Lotek adoption (docs/LOTEK_ADOPTION.md §4):
``current_actor_username``, ``host_can_write``, and the model/enum resolvers ``client_model`` /
``severity_enum`` (§3.1/§3.2, PLAN.md §19).

``current_actor_username``/``host_can_write`` wrap an OPTIONAL callable the host injects on
``ScribbleConfig.extras`` (scribble/config.py:25). Absent hook, a hook returning ``None``/an object with
no ``.username``, and a RAISING hook must all degrade safely -- these back attribution (``created_by``)
and a UI nudge, never enforcement, so a misbehaving host hook must never break a write or wrongly lock
out a writable standalone install. Mirrors the existing extras-hook test pattern already used for
``collab_authorize`` in tests/test_collab.py.

``client_model``/``severity_enum`` instead wrap a plain ``ScribbleConfig`` field (not an ``extras``
callable): the host passes a MODEL/ENUM directly to ``register(..., client_model=..., severity_enum=...)``
rather than a hook function, so these resolvers just return ``cfg.client_model or <scribble's own>`` /
``cfg.severity_enum or <scribble's own>`` -- see scribble/deps.py.
"""

from __future__ import annotations

import enum
from types import SimpleNamespace

from scribble.deps import (
    client_model,
    current_actor_id,
    current_actor_username,
    host_can_write,
    severity_enum,
)
from scribble.enums import Severity
from scribble.models import Client

# --------------------------------------------------------------------------------- current_actor_username


def test_current_actor_username_none_without_host_hook(app):
    # Standalone Scribble (no host, no extras entry): always None.
    with app.app_context():
        assert current_actor_username() is None


def test_current_actor_username_reads_hook_username(app):
    cfg = app.extensions["scribble"]
    cfg.extras["current_actor"] = lambda: SimpleNamespace(username="j.analyst")
    try:
        with app.app_context():
            assert current_actor_username() == "j.analyst"
    finally:
        cfg.extras.pop("current_actor", None)


def test_current_actor_id_none_without_host_hook(app):
    # Standalone Scribble: no host actor -> owner_id attribution is None.
    with app.app_context():
        assert current_actor_id() is None


def test_current_actor_id_reads_hook_id(app):
    cfg = app.extensions["scribble"]
    cfg.extras["current_actor"] = lambda: SimpleNamespace(id=42, username="j.analyst")
    try:
        with app.app_context():
            assert current_actor_id() == 42
    finally:
        cfg.extras.pop("current_actor", None)


def test_current_actor_id_none_when_id_not_int(app):
    # A user object with a non-int id (or none) -> None, never a bad owner_id write.
    cfg = app.extensions["scribble"]
    cfg.extras["current_actor"] = lambda: SimpleNamespace(id="not-an-int")
    try:
        with app.app_context():
            assert current_actor_id() is None
    finally:
        cfg.extras.pop("current_actor", None)


def test_current_actor_username_none_when_hook_returns_none(app):
    # Host hook present but nobody is logged in (e.g. an anonymous/system context).
    cfg = app.extensions["scribble"]
    cfg.extras["current_actor"] = lambda: None
    try:
        with app.app_context():
            assert current_actor_username() is None
    finally:
        cfg.extras.pop("current_actor", None)


def test_current_actor_username_none_when_actor_has_no_username_attr(app):
    cfg = app.extensions["scribble"]
    cfg.extras["current_actor"] = lambda: object()
    try:
        with app.app_context():
            assert current_actor_username() is None
    finally:
        cfg.extras.pop("current_actor", None)


def test_current_actor_username_none_when_hook_raises(app):
    cfg = app.extensions["scribble"]

    def boom():
        raise RuntimeError("host session backend down")

    cfg.extras["current_actor"] = boom
    try:
        with app.app_context():
            # A throwing host hook must not propagate -- created_by threading downstream would
            # otherwise turn a host-side outage into a 500 on every Scribble write.
            assert current_actor_username() is None
    finally:
        cfg.extras.pop("current_actor", None)


# --------------------------------------------------------------------------------- host_can_write


def test_host_can_write_true_without_host_hook(app):
    # Standalone Scribble is always writable.
    with app.app_context():
        assert host_can_write() is True


def test_host_can_write_reflects_hook_true(app):
    cfg = app.extensions["scribble"]
    cfg.extras["can_write"] = lambda: True
    try:
        with app.app_context():
            assert host_can_write() is True
    finally:
        cfg.extras.pop("can_write", None)


def test_host_can_write_reflects_hook_false(app):
    cfg = app.extensions["scribble"]
    cfg.extras["can_write"] = lambda: False
    try:
        with app.app_context():
            assert host_can_write() is False
    finally:
        cfg.extras.pop("can_write", None)


def test_host_can_write_true_when_hook_raises(app):
    cfg = app.extensions["scribble"]

    def boom():
        raise RuntimeError("host RBAC backend down")

    cfg.extras["can_write"] = boom
    try:
        with app.app_context():
            # Fail OPEN: this only drives a UI nudge (hide/disable controls), never the real
            # enforcement, so a misbehaving hook must never lock a standalone-safe write out.
            assert host_can_write() is True
    finally:
        cfg.extras.pop("can_write", None)


def test_host_can_write_coerces_truthy_return_value(app):
    cfg = app.extensions["scribble"]
    cfg.extras["can_write"] = lambda: 0  # falsy non-bool
    try:
        with app.app_context():
            assert host_can_write() is False
    finally:
        cfg.extras.pop("can_write", None)


# --------------------------------------------------------------------------------------- client_model


class _HostClient:
    """A stand-in for a host-injected client model (e.g. Lotek's ``Client``) -- only needs to be a
    distinct object identity from ``scribble.models.Client`` for these resolver-level tests; nothing
    here touches a database."""


def test_client_model_defaults_to_scribbles_own_standalone(app):
    # Standalone Scribble (register() called with no client_model, as tests/conftest.py's `app` fixture
    # does): the resolver falls back to scribble.models.Client.
    with app.app_context():
        assert client_model() is Client


def test_client_model_returns_the_injected_host_model_when_mounted(app):
    cfg = app.extensions["scribble"]
    cfg.client_model = _HostClient
    try:
        with app.app_context():
            assert client_model() is _HostClient
    finally:
        cfg.client_model = None


# -------------------------------------------------------------------------------------- severity_enum


def test_severity_enum_defaults_to_scribbles_own_standalone(app):
    with app.app_context():
        assert severity_enum() is Severity


def test_severity_enum_returns_the_injected_host_enum_when_mounted(app):
    class _HostSeverity(enum.StrEnum):
        """Value-identical to scribble.enums.Severity (docs/LOTEK_ADOPTION.md §3.2) but a DIFFERENT
        class -- that's the point: the resolver must return the injected object, not merely an
        equal-valued one."""

        info = "info"
        low = "low"
        medium = "medium"
        high = "high"
        critical = "critical"

    cfg = app.extensions["scribble"]
    cfg.severity_enum = _HostSeverity
    try:
        with app.app_context():
            resolved = severity_enum()
            assert resolved is _HostSeverity
            assert resolved is not Severity
    finally:
        cfg.severity_enum = None
