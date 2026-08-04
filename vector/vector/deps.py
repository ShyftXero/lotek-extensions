"""Runtime accessors for blueprint/API views: the mounted config, a session context manager, and the
optional host-capability hooks (current actor, can-write) that arrive through ``config.extras``.

Every host hook is optional and fail-safe: standalone Vector (no host) resolves them to safe defaults,
and a misbehaving host hook must never break the write/read it decorates. The host's own request-method /
role gate is the real write enforcement — the ``can_write`` hook here only drives UI nudges.
"""

from __future__ import annotations

from contextlib import contextmanager

from flask import current_app

from vector.config import VectorConfig


def get_config() -> VectorConfig:
    cfg = current_app.extensions.get("vector")
    if cfg is None:  # pragma: no cover - misconfiguration guard
        raise RuntimeError("Vector is not registered on this app (call vector.register).")
    return cfg


@contextmanager
def open_session():
    """Yield a SQLAlchemy session bound to the mounted engine (session-per-request pattern)."""
    cfg = get_config()
    session = cfg.session_factory()
    try:
        yield session
    finally:
        session.close()


def _actor():
    """The host's logged-in user object (or None), via the optional ``extras['current_actor']`` hook."""
    try:
        cfg = get_config()
    except RuntimeError:  # pragma: no cover - defensive; no app context
        return None
    hook = cfg.extras.get("current_actor")
    if hook is None:
        return None
    try:
        return hook()
    except Exception:  # noqa: BLE001 - a throwing host hook must not break the request it decorates
        return None


def current_actor_username() -> str | None:
    """The logged-in host user's username (``created_by`` attribution), or None. Standalone -> None."""
    actor = _actor()
    return getattr(actor, "username", None) if actor is not None else None


def current_actor_id() -> int | None:
    """The logged-in host user's id (``owner_id`` scope + attribution), or None. Standalone -> None."""
    actor = _actor()
    ident = getattr(actor, "id", None)
    return ident if isinstance(ident, int) else None


def current_actor_is_admin() -> bool:
    """Whether the current host user is an admin (sees every diagram, incl. legacy NULL-owner rows).

    Reads a common shape of the injected actor: ``actor.role`` with an ``is_admin``/``== 'admin'``
    signal, or an ``actor.is_admin`` bool. Standalone (no host actor) is treated as admin — a
    single-user local tool has no one else to scope against. Fail-safe: any error -> False (mounted) so
    a broken hook can never widen visibility beyond the owner.
    """
    actor = _actor()
    if actor is None:
        # Standalone: no host identity, single local user — full visibility.
        try:
            return get_config().extras.get("current_actor") is None
        except RuntimeError:  # pragma: no cover
            return True
    try:
        role = getattr(actor, "role", None)
        if role is not None:
            if getattr(role, "is_admin", None) is True:
                return True
            val = getattr(role, "value", role)
            if isinstance(val, str) and val.lower() == "admin":
                return True
        return bool(getattr(actor, "is_admin", False))
    except Exception:  # noqa: BLE001
        return False


def host_can_write() -> bool:
    """Whether the current user may mutate, per the optional ``extras['can_write']`` hook (``() -> bool``).

    Absent/raising hook -> True (standalone is always writable); this only drives a UI nudge, the host's
    role gate is the real enforcement, so failing open never opens a hole.
    """
    try:
        cfg = get_config()
    except RuntimeError:  # pragma: no cover - defensive; no app context
        return True
    hook = cfg.extras.get("can_write")
    if hook is None:
        return True
    try:
        return bool(hook())
    except Exception:  # noqa: BLE001 - a throwing host hook must never block standalone-safe writes
        return True


def client_model():
    """The ``Client`` model this mounted Vector queries against — the host-injected one when present,
    else Vector's own ``vector.models.Client`` (standalone)."""
    from vector.models import Client  # local import: avoids a models.py <-> deps.py import cycle

    try:
        cfg = get_config()
    except RuntimeError:  # pragma: no cover - defensive; no app context
        return Client
    return cfg.client_model or Client
