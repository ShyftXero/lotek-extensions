"""Runtime accessors for blueprint/API views: the mounted config, a session context manager, and the
optional host-capability hooks (current actor, can-write) that arrive through ``config.extras``.

Every host hook is optional and fail-safe: standalone Vector (no host) resolves them to safe defaults,
and a misbehaving host hook must never break the write/read it decorates. The host's own request-method /
role gate is the real write enforcement — the ``can_write`` hook here only drives UI nudges.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from typing import Any

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


def current_actor_id() -> uuid.UUID | None:
    """The logged-in host user's id (``owner_id`` scope + attribution), or None. Standalone -> None.

    lotek's core keys ``User`` on UUIDv7 (v2), so the host actor's ``id`` is a ``uuid.UUID``. Guarding on
    ``uuid.UUID`` — not the old ``int`` — is load-bearing: an ``isinstance(ident, int)`` guard silently
    returned None for every mounted diagram (unattributed owner + a ``owner_id == None`` visibility
    filter), the exact silent-degradation the v2 model exists to avoid.
    """
    actor = _actor()
    ident = getattr(actor, "id", None)
    if isinstance(ident, uuid.UUID):
        return ident
    if ident is not None:
        # Degrade LOUDLY, never silently: a non-UUID host id is the same silent-degradation shape the
        # UUID guard replaced (the old int guard returned None for a uuid.UUID host id -> owner-loss).
        # Returning None here is the UNSAFE value in mounted mode (null-owner rows read as universally
        # visible), so surface it rather than swallow it.
        logging.getLogger("vector").warning(
            "current_actor_id: host actor id is %s, not a uuid.UUID; owner scope will be None",
            type(ident).__name__,
        )
    return None


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


def host_setting(key: str, default: Any = None) -> Any:
    """One ADMIN-scope setting the host holds for Vector, via ``extras['extension_setting']``.

    These are the ``[[settings]]`` Vector declares in ``lotek-extension.toml`` (lotek#485). The HOST
    owns the form, the admin gate, the storage and the audit row — Vector only reads. Standalone (no
    host) resolves to ``default``, and any error does too: a settings lookup must never be the thing
    that breaks the export it decorates.

    NOT for a per-USER preference. Those are Vector's own ``vector_user_prefs`` rows behind Vector's
    own cog — they cross no privilege boundary, so the host has no business holding them.
    """
    try:
        cfg = get_config()
    except RuntimeError:  # pragma: no cover - defensive; no app context
        return default
    hook = cfg.extras.get("extension_setting")
    if hook is None:
        return default
    try:
        value = hook(key, default)
    except Exception:  # noqa: BLE001 - a throwing host hook must not break the request it decorates
        return default
    return default if value is None else value


def host_is_mounted() -> bool:
    """Whether a host capability bundle was injected (``extras['host']``). Standalone Vector has no host
    tenancy model at all, so the engagement predicates below degrade to the owner-scope fallback."""
    try:
        return bool(get_config().extras.get("host"))
    except RuntimeError:  # pragma: no cover - defensive; no app context
        return False


def host_can_view_engagement(engagement_id) -> bool:
    """Does the CURRENT request's principal hold a live observer-or-better membership on this engagement?

    Asked of the host's ``extras['can_view_engagement']`` hook (``(engagement_id) -> bool``), which reads
    the request principal itself — so it answers for BOTH the cookie and the PAT surface without either
    passing an actor. Fails CLOSED (False) when a host is mounted but injected no hook: an engagement-bound
    row with no way to check membership must not be shown. Standalone (no host) also returns False, but a
    standalone diagram is never engagement-bound so this is never reached there.
    """
    hook = _extras().get("can_view_engagement")
    if hook is None:
        return False
    try:
        return bool(hook(engagement_id))
    except Exception:  # noqa: BLE001 - a throwing host hook fails closed, never widens visibility
        return False


def host_can_operate_on(engagement_id) -> bool:
    """Does the current principal hold a live OPERATOR capability on this engagement? (INV-TENANCY-05.)

    The WRITE gate for an engagement-bound diagram — asked of ``extras['can_operate_on']``, never
    ``can_write()`` (the global ceiling) which the invariant's red-path forbids as the object gate. Fails
    closed."""
    hook = _extras().get("can_operate_on")
    if hook is None:
        return False
    try:
        return bool(hook(engagement_id))
    except Exception:  # noqa: BLE001 - fail closed
        return False


def host_visible_engagement_ids():
    """The scoped SET of engagement ids the current principal may see, or ``None`` when unavailable
    (standalone / a host bundle predating the hook). ``None`` is distinct from an EMPTY set: empty means
    "this actor holds nothing", None means "no set to scope by" — the list statement treats both as
    "no engagement-bound rows are visible" (fail closed), which is safe because a standalone/unmounted
    Vector has no engagement-bound rows anyway."""
    hook = _extras().get("visible_engagement_ids")
    if hook is None:
        return None
    try:
        return frozenset(hook())
    except Exception:  # noqa: BLE001 - fail closed: no set -> no bound rows shown
        return None


def host_audit(db, verb: str, *, subject_type: str, subject_id=None, before=None, after=None) -> None:
    """Append one ``ext:vector:<verb>`` row through the host audit seam (INV-AUDIT-03), in the SAME
    session/txn as the change (``db``) so it commits atomically. No-op standalone (no host). ``subject_id``
    is coerced to ``str`` — the host's column is text and a route serializes the same id as a string, so a
    raw ``uuid.UUID`` would never correlate against the API response (the scribble fix, #256)."""
    hook = _extras().get("audit")
    if hook is None:
        return
    hook(db, f"ext:vector:{verb}", subject_type=subject_type,
         subject_id=None if subject_id is None else str(subject_id), before=before, after=after)


def _extras() -> dict:
    try:
        return get_config().extras or {}
    except RuntimeError:  # pragma: no cover - defensive; no app context
        return {}


def client_model():
    """The ``Client`` model this mounted Vector queries against — the host-injected one when present,
    else Vector's own ``vector.models.Client`` (standalone)."""
    from vector.models import Client  # local import: avoids a models.py <-> deps.py import cycle

    try:
        cfg = get_config()
    except RuntimeError:  # pragma: no cover - defensive; no app context
        return Client
    return cfg.client_model or Client
