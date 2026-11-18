"""Runtime accessors — mounted config + optional host-capability hooks via ``config.extras``.

Every hook is optional. Standalone (no host) resolves to safe single-local-user defaults;
MOUNTED, every authorization-relevant accessor fails closed.

The asymmetry in ``current_actor_is_admin`` is deliberate:
* no ``current_actor`` hook at all → standalone → admin.
* a hook that returns None → mounted, nobody logged in → NOT admin.
"""

from __future__ import annotations

import logging
import uuid

from flask import current_app

from pastel.config import PastelConfig


def get_config() -> PastelConfig:
    cfg = current_app.extensions.get("pastel")
    if cfg is None:  # pragma: no cover
        raise RuntimeError("Pastel is not registered on this app (call pastel.register).")
    return cfg


def _extras() -> dict:
    try:
        return get_config().extras or {}
    except RuntimeError:  # pragma: no cover
        return {}


_warned_standalone = False


def is_standalone() -> bool:
    global _warned_standalone
    standalone = _extras().get("current_actor") is None
    if standalone and not _warned_standalone:
        _warned_standalone = True
        logging.getLogger("pastel").warning(
            "no host `current_actor` hook: running STANDALONE — the local user is treated as admin. "
            "If this is a mounted deployment, the host failed to inject its identity hook."
        )
    return standalone


def _actor():
    hook = _extras().get("current_actor")
    if hook is None:
        return None
    try:
        return hook()
    except Exception:  # noqa: BLE001
        return None


def current_actor_id() -> uuid.UUID | None:
    ident = getattr(_actor(), "id", None)
    if isinstance(ident, uuid.UUID):
        return ident
    if ident is not None:
        logging.getLogger("pastel").warning(
            "current_actor_id: host actor id is %s, not uuid.UUID; treating caller as anonymous",
            type(ident).__name__,
        )
    return None


def current_actor_username() -> str | None:
    actor = _actor()
    return getattr(actor, "username", None) if actor is not None else None


def current_actor_is_admin() -> bool:
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
        return getattr(actor, "is_admin", False) is True
    except Exception:  # noqa: BLE001
        return False


def host_can_write() -> bool:
    hook = _extras().get("can_write")
    if hook is None:
        return True
    try:
        return bool(hook())
    except Exception:  # noqa: BLE001
        return True


def host_audit():
    """The host's audited-write callable, or None standalone. NOT swallowed (INV-AUDIT-03)."""
    return _extras().get("audit")
