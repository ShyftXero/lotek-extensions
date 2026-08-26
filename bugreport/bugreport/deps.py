"""Runtime accessors: the mounted config + the optional host-capability hooks that arrive through
``config.extras``.

Every hook is optional. Standalone Bugreport (no host) resolves them to safe single-local-user defaults;
MOUNTED, every authorization-relevant accessor **fails closed**.

The asymmetry in :func:`current_actor_is_admin` is the fail-closed line and is deliberate:

* **no ``current_actor`` hook at all** -> standalone, single local user -> admin.
* **a hook that returns ``None``** -> mounted but nobody is logged in -> NOT admin, and
  :func:`current_actor_id` is ``None``, so the owner filter matches no row. Anonymous sees nothing.

Do not "simplify" those two cases into one.
"""

from __future__ import annotations

import logging
import uuid

from flask import current_app

from bugreport.config import BugreportConfig


def get_config() -> BugreportConfig:
    cfg = current_app.extensions.get("bugreport")
    if cfg is None:  # pragma: no cover - misconfiguration guard
        raise RuntimeError("Bugreport is not registered on this app (call bugreport.register).")
    return cfg


def _extras() -> dict:
    try:
        return get_config().extras or {}
    except RuntimeError:  # pragma: no cover
        return {}


_warned_standalone = False


def is_standalone() -> bool:
    """True when no host injected an identity hook — a single local user who is their own admin.

    This is the ONE input whose ABSENCE widens access (no hook -> admin -> every report visible), so a
    host that forgot to inject ``current_actor`` would fail OPEN and do it mutely. It cannot happen under
    lotek (``extensions._inject_host`` always sets it), which is exactly why it would go unnoticed
    somewhere else — so say so, once, at WARNING."""
    global _warned_standalone
    standalone = _extras().get("current_actor") is None
    if standalone and not _warned_standalone:
        _warned_standalone = True
        logging.getLogger("bugreport").warning(
            "no host `current_actor` hook: running STANDALONE — the local user is treated as admin and "
            "sees every report. If this is a mounted deployment, the host failed to inject its identity "
            "hook and reports are NOT tenancy-scoped."
        )
    return standalone


def _actor():
    hook = _extras().get("current_actor")
    if hook is None:
        return None
    try:
        return hook()
    except Exception:  # noqa: BLE001 - a throwing host hook must not break the request it decorates
        return None


def current_actor_id() -> uuid.UUID | None:
    """The logged-in host user's id — the tenancy key for a report — or None standalone/anonymous.

    lotek keys ``User`` on UUIDv7, so a mounted actor's ``id`` is a ``uuid.UUID``. Degrade LOUDLY on a
    surprise type instead of silently returning None (a silent None is the exact degradation the v2 model
    exists to avoid; same guard as registrar/vector)."""
    ident = getattr(_actor(), "id", None)
    if isinstance(ident, uuid.UUID):
        return ident
    if ident is not None:
        logging.getLogger("bugreport").warning(
            "current_actor_id: host actor id is %s, not a uuid.UUID; treating the caller as anonymous",
            type(ident).__name__,
        )
    return None


def current_actor_username() -> str | None:
    actor = _actor()
    return getattr(actor, "username", None) if actor is not None else None


def current_actor_is_admin() -> bool:
    """Admin signal off the injected actor. Standalone -> True. Mounted with no/unknown actor -> False.
    Any error -> False, so a broken hook never widens access."""
    actor = _actor()
    if actor is None:
        return is_standalone()
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
    Only drives a UI nudge — the host's own role gate is the real enforcement, so failing open opens no
    hole here."""
    hook = _extras().get("can_write")
    if hook is None:
        return True
    try:
        return bool(hook())
    except Exception:  # noqa: BLE001
        return True


def host_audit():
    """The host's audited-write callable (``(db, action, *, subject_type, subject_id, before, after)``),
    or None standalone. NOT swallowed: an audit failure must abort the action it records (INV-AUDIT-03),
    so the caller runs it inside the same transaction as the change."""
    return _extras().get("audit")
