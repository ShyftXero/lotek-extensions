"""Runtime accessors: the mounted config + the optional host-capability hooks (current actor, can-write)
that arrive through ``config.extras``.

Every host hook is optional and fail-safe: standalone CREAM resolves them to safe defaults, and a
misbehaving host hook never breaks the request it decorates. The host's own role gate is the real write
enforcement — the ``can_write`` hook only drives UI nudges.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager

from flask import current_app

from cream.config import CreamConfig


def get_config() -> CreamConfig:
    cfg = current_app.extensions.get("cream")
    if cfg is None:  # pragma: no cover - misconfiguration guard
        raise RuntimeError("CREAM is not registered on this app (call cream.register).")
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
        logging.getLogger("cream").warning(
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


def host_audit(db, verb: str, *, subject_type: str, subject_id=None, before=None, after=None) -> None:
    """Append one ``ext:cream:<verb>`` row through the host audit seam (INV-AUDIT-03), in the SAME
    session/txn as the change (``db``) so it commits atomically with it — a business state change that
    rolls back leaves no audit row, and one that commits always leaves exactly one. No-op standalone (no
    host audit trail). ``subject_id`` is coerced to ``str`` — the host column is text and a route
    serializes the same id as a string (the scribble fix, #256), so a raw ``uuid.UUID`` would never
    correlate against the API response.

    This is the per-entity trail the coarse ``EXTENSION_MACHINE_WRITE`` backstop cannot give: it names the
    document/brand and, for a lifecycle transition or a brand edit, the before/after — the precondition for
    ever noticing an invoice-fraud remittance redirect (``payment_instructions``)."""
    hook = _extras().get("audit")
    if hook is None:
        return
    hook(db, f"ext:cream:{verb}", subject_type=subject_type,
         subject_id=None if subject_id is None else str(subject_id), before=before, after=after)


def host_can_operate_on(engagement_id: uuid.UUID) -> bool:
    """Operator capability on ``engagement_id`` for the current principal, via the host's
    ``can_operate_on`` seam (INV-TENANCY-05). Standalone (no host hook) is a single local user -> True.
    Mounted: the host answer, and any error fails CLOSED (a document must never be created against an
    engagement the actor cannot operate on)."""
    hook = _extras().get("can_operate_on")
    if hook is None:
        return True
    try:
        return bool(hook(engagement_id))
    except Exception:  # noqa: BLE001 - mounted authorization failure fails closed
        return False


def host_visible_engagement_ids():
    """The engagements the current principal may read (to scope list queries), or ``None`` standalone
    (no scoping). Mounted error -> empty set (fail closed: show nothing rather than everything)."""
    hook = _extras().get("visible_engagement_ids")
    if hook is None:
        return None
    try:
        return hook()
    except Exception:  # noqa: BLE001
        return frozenset()


def host_engagement_scope(engagement_id: uuid.UUID) -> list[str]:
    """The engagement's real targets — CIDRs, hosts, URLs, AD domains — via the optional
    ``extras['engagement_scope']`` hook.

    This is what lets a quote's Appendix A be the scope-of-record instead of a hand-typed list that
    drifts from what the scanner was actually pointed at. Absent (standalone) or raising -> ``[]``: an
    empty appendix is a visibly incomplete document, whereas a partial one would look authoritative.
    """
    hook = _extras().get("engagement_scope")
    if hook is None:
        return []
    try:
        return [str(t) for t in (hook(engagement_id) or [])]
    except Exception:  # noqa: BLE001 - a host that cannot answer yields no scope, never a 500
        logging.getLogger("cream").warning("engagement_scope hook failed for %s", engagement_id)
        return []


def host_engagement_units(engagement_id: uuid.UUID) -> list[str]:
    """The billable ``unit_key``s the engagement currently contains (run types, phases, scope bands),
    via the optional ``extras['engagement_units']`` hook. Absent/raising -> ``[]``.

    Without this the rate-card sync is only callable by something that already knows the engagement's
    shape — which the browser does not. The hook is what makes "suggest what is not yet billed" a button
    a human can press rather than an API for the host to drive.
    """
    hook = _extras().get("engagement_units")
    if hook is None:
        return []
    try:
        return [str(k) for k in (hook(engagement_id) or [])]
    except Exception:  # noqa: BLE001
        logging.getLogger("cream").warning("engagement_units hook failed for %s", engagement_id)
        return []


def host_engagement_burn(engagement_id: uuid.UUID) -> dict:
    """Measured execution per billable unit — ``{unit_key: quantity}`` — via the optional
    ``extras['engagement_burn']`` hook. Absent/raising -> ``{}``.

    Advisory input to the quoted-vs-executed view in the editor. It never reaches a client-facing
    document and never changes a price; the host's numbers are evidence for a human, not a billing feed.
    """
    hook = _extras().get("engagement_burn")
    if hook is None:
        return {}
    try:
        got = hook(engagement_id)
        return dict(got) if got else {}
    except Exception:  # noqa: BLE001
        logging.getLogger("cream").warning("engagement_burn hook failed for %s", engagement_id)
        return {}
