"""Runtime accessors: the mounted config + the optional host-capability hooks (current actor, can-write)
that arrive through ``config.extras``.

Every host hook is optional and fail-safe: standalone REGISTRAR resolves them to safe defaults, and a
misbehaving host hook never breaks the request it decorates. The host's own role gate is the real write
enforcement — the ``can_write`` hook only drives UI nudges.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager

from flask import current_app

from registrar.config import RegistrarConfig


def get_config() -> RegistrarConfig:
    cfg = current_app.extensions.get("registrar")
    if cfg is None:  # pragma: no cover - misconfiguration guard
        raise RuntimeError("REGISTRAR is not registered on this app (call registrar.register).")
    return cfg


@contextmanager
def open_session():
    cfg = get_config()
    session = cfg.session_factory()
    try:
        yield session
    finally:
        session.close()


def _actor():
    try:
        cfg = get_config()
    except RuntimeError:  # pragma: no cover
        return None
    hook = cfg.extras.get("current_actor")
    if hook is None:
        return None
    try:
        return hook()
    except Exception:  # noqa: BLE001 - a throwing host hook must not break the request it decorates
        return None


def current_actor_id() -> uuid.UUID | None:
    """The logged-in host user's id (attribution/scope), or None standalone.

    lotek keys ``User`` on UUIDv7, so a mounted actor's ``id`` is a ``uuid.UUID``. Degrade LOUDLY on a
    surprise type instead of silently returning None (a silent None is the exact degradation the v2
    model exists to avoid; see the Vector extension's identical guard)."""
    actor = _actor()
    ident = getattr(actor, "id", None)
    if isinstance(ident, uuid.UUID):
        return ident
    if ident is not None:
        logging.getLogger("registrar").warning(
            "current_actor_id: host actor id is %s, not a uuid.UUID; attribution will be None",
            type(ident).__name__,
        )
    return None


def current_actor_username() -> str | None:
    actor = _actor()
    return getattr(actor, "username", None) if actor is not None else None


def current_actor_is_admin() -> bool:
    """Admin signal off the injected actor; standalone (no host actor) is treated as admin (single local
    user). Fail-safe: any error -> False when mounted, so a broken hook never widens access."""
    actor = _actor()
    if actor is None:
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
    """Per the optional ``extras['can_write']`` hook; absent/raising -> True (standalone always writable).
    Only drives a UI nudge — the host role gate is the real enforcement, so failing open opens no hole."""
    try:
        cfg = get_config()
    except RuntimeError:  # pragma: no cover
        return True
    hook = cfg.extras.get("can_write")
    if hook is None:
        return True
    try:
        return bool(hook())
    except Exception:  # noqa: BLE001
        return True


def _extras() -> dict:
    try:
        return get_config().extras or {}
    except RuntimeError:  # pragma: no cover
        return {}


def host_can_operate_on(engagement_id: uuid.UUID) -> bool:
    """Operator capability on ``engagement_id`` for the current principal, via the host seam
    (INV-TENANCY-05). Standalone -> True; mounted error -> False (fail closed)."""
    hook = _extras().get("can_operate_on")
    if hook is None:
        return True
    try:
        return bool(hook(engagement_id))
    except Exception:  # noqa: BLE001 - mounted authorization failure fails closed
        return False


def host_visible_engagement_ids():
    """Engagements the current principal may read (to scope transient-server lists), or ``None``
    standalone (no scoping). Mounted error -> empty set (fail closed)."""
    hook = _extras().get("visible_engagement_ids")
    if hook is None:
        return None
    try:
        return hook()
    except Exception:  # noqa: BLE001
        return frozenset()


def host_audit():
    """The host's audited-write callable (``(db, action, *, subject_type, subject_id, before, after)``),
    or None standalone. NOT swallowed: an audit failure must abort the action (defensibility) — the
    caller runs it inside the same transaction as the change."""
    return _extras().get("audit")


def host_is_interactive() -> bool:
    """Whether the current request is an interactive dashboard session (a human), via the host seam.
    Standalone -> True (single local user). Mounted error -> False (fail closed: not interactive)."""
    hook = _extras().get("is_interactive")
    if hook is None:
        return True
    try:
        return bool(hook())
    except Exception:  # noqa: BLE001
        return False
