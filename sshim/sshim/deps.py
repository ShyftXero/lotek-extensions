"""Runtime accessors: the mounted config + the optional host-capability hooks that arrive through
``config.extras``.

Every hook is optional. Standalone sshim (no host) resolves them to safe single-local-user defaults;
MOUNTED, every authorization-relevant accessor **fails closed**. Mirrors ``bugreport/deps.py``.

The asymmetry in :func:`current_actor_is_admin` is the fail-closed line and is deliberate:

* **no ``current_actor`` hook at all** -> standalone, single local user -> admin.
* **a hook that returns ``None``** -> mounted but nobody is logged in -> NOT admin, and
  :func:`current_actor_id` is ``None``, so the owner filter matches no row. Anonymous sees nothing.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from flask import current_app

from sshim import secretbox
from sshim.config import SshimConfig

#: Admin-scope `[[settings]]` defaults + bounds.
#:
#: `DEFAULT_RELAY_HOST` is deliberately EMPTY. sshim ships no rendezvous default: a fixed host on a
#: well-known inbound port is the one thing this design must not hand an operator, and Phase 1's
#: rendezvous is the operator's OWN box, so any shipped default would point at a resource that only
#: exists in Phase 2. The mint page opens with a blank rendezvous field the operator fills in; a blank
#: one fails validation with "configure a rendezvous host".
#:
#: `MAX_TTL_MINUTES` is the hard ceiling on a session TTL, re-clamped here (see `max_ttl_minutes`) so a
#: value the host hands back cannot widen the cap past what the manifest declares.
DEFAULT_RELAY_HOST = ""
MAX_TTL_MINUTES = 240
MAX_TTL_MINUTES_BOUNDS = (1, 1440)


def get_config() -> SshimConfig:
    cfg = current_app.extensions.get("sshim")
    if cfg is None:  # pragma: no cover - misconfiguration guard
        raise RuntimeError("sshim is not registered on this app (call sshim.register).")
    return cfg


def _extras() -> dict:
    try:
        return get_config().extras or {}
    except RuntimeError:  # pragma: no cover
        return {}


def host_hook(name: str):
    """Single accessor for ANY host-injected capability, resolved late per request.

    This is also the Phase-2 seam: when lotek#1747 lands ``exclusive_container_acquire`` /
    ``exclusive_container_release`` in ``extras``, they are read through here (see the two documented,
    UNUSED accessors below) — no other wiring changes. Phase 1 reads only the identity/audit/settings/PAT
    hooks that follow.
    """
    return _extras().get(name)


# ── Phase-2 container seam (lotek#1747) — documented, NOT called by Phase 1 ───
#
# Phase 1 emits commands for an operator's OWN box and calls neither hook. These accessors exist so the
# next slice has exactly one obvious place to land and nothing has to be re-wired then: the host injects
# the capability into `extras`, `host_hook` resolves it late, and no core import is added.

PHASE2_EXCLUSIVE_ACQUIRE = "exclusive_container_acquire"
PHASE2_EXCLUSIVE_RELEASE = "exclusive_container_release"


def exclusive_container_acquire():
    """Phase-2 ONLY — never called in Phase 1. The host's ``exclusive_container_acquire`` hook
    (lotek#1747), or ``None`` when the host does not provide it.

    When present, a mint asks the host for a core-hosted single-use relay instead of rendering a command
    for the operator's own box; the matching teardown goes through
    :func:`exclusive_container_release`. Documented and wired here, deliberately unexercised.
    """
    return host_hook(PHASE2_EXCLUSIVE_ACQUIRE)


def exclusive_container_release():
    """Phase-2 ONLY — never called in Phase 1. Pairs with :func:`exclusive_container_acquire`: the host
    hook that hands back (or tears down) the core-hosted single-use relay a minted pivot would run on."""
    return host_hook(PHASE2_EXCLUSIVE_RELEASE)


_warned_standalone = False


def is_standalone() -> bool:
    """True when no host injected an identity hook — a single local user who is their own admin.

    This is the ONE input whose ABSENCE widens access (no hook -> admin -> every session visible), so a
    host that forgot to inject ``current_actor`` would fail OPEN and do it mutely. It cannot happen under
    lotek, which is exactly why it would go unnoticed elsewhere — so say so, once, at WARNING."""
    global _warned_standalone
    standalone = _extras().get("current_actor") is None
    if standalone and not _warned_standalone:
        _warned_standalone = True
        logging.getLogger("sshim").warning(
            "no host `current_actor` hook: running STANDALONE — the local user is treated as admin and "
            "sees every pivot. If this is a mounted deployment, the host failed to inject its identity "
            "hook and sessions are NOT tenancy-scoped."
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
    """The logged-in host user's id — the tenancy key for a session/profile — or None standalone/anon.

    lotek keys ``User`` on UUIDv7, so a mounted actor's ``id`` is a ``uuid.UUID``. Degrade LOUDLY on a
    surprise type instead of silently returning None."""
    ident = getattr(_actor(), "id", None)
    if isinstance(ident, uuid.UUID):
        return ident
    if ident is not None:
        logging.getLogger("sshim").warning(
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
        # `is True`, NOT `bool(...)`: a bound `is_admin()` METHOD is truthy and would make every user an
        # admin. This predicate is the only thing widening a read past its owner.
        return getattr(actor, "is_admin", False) is True
    except Exception:  # noqa: BLE001
        return False


def secret_key_path() -> Path:
    """Path of the instance-dir key that SEALS each session's stored password (``sshim.secretbox``).

    One home for "where the key lives": it sits beside the instance (0700 dir, 0600 file created on the
    first seal), never in the DB and never in a log. Callers that seal or unseal a secret take this as an
    explicit argument rather than reaching for the app themselves, which is what keeps
    ``sshim.service`` Flask-free."""
    return secretbox.key_path(get_config().instance_path)


def host_audit():
    """The host's audited-write callable, or None standalone. NOT swallowed by callers: an audit failure
    must abort the action it records (INV-AUDIT-03), so the caller runs it in the same transaction."""
    return _extras().get("audit")


def host_setting(key: str, default=None):
    """One ADMIN-scope setting the host holds for sshim, via ``extras['extension_setting']`` (lotek#485).

    The HOST owns the form, the admin gate, the storage and the audit row — sshim only reads. Standalone
    and any error resolve to ``default``: a settings lookup must never break the request it configures.
    A caller consuming a security bound still clamps the result (see :func:`max_ttl_minutes`)."""
    hook = _extras().get("extension_setting")
    if hook is None:
        return default
    try:
        value = hook(key, default)
    except Exception:  # noqa: BLE001
        return default
    return default if value is None else value


def default_relay_host() -> str:
    """The rendezvous host the mint page opens to (``default_relay_host`` setting, else the shipped
    default). Empty by default — ``""`` means "no rendezvous configured yet", which the mint page renders
    as a blank field the operator fills, and the plan validator rejects until they name their own box."""
    raw = host_setting("default_relay_host", DEFAULT_RELAY_HOST)
    return raw.strip() if isinstance(raw, str) and raw.strip() else DEFAULT_RELAY_HOST


def max_ttl_minutes() -> int:
    """Hard ceiling on a session TTL in minutes (``max_ttl_minutes`` setting, coerced + clamped).

    Clamped here because it bounds how long an outward pivot capability stays live; a stale/garbage
    value degrades to the shipped default rather than to an unbounded TTL."""
    raw = host_setting("max_ttl_minutes", MAX_TTL_MINUTES)
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return MAX_TTL_MINUTES
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return MAX_TTL_MINUTES
    if value != value:  # noqa: PLR0124 — NaN guard without importing math
        return MAX_TTL_MINUTES
    lo, hi = MAX_TTL_MINUTES_BOUNDS
    return min(max(value, lo), hi)


def relay_connected(session) -> bool | None:
    """Is a leg currently dialled into this session's relay: ``True`` / ``False`` / ``None`` (unknown).

    Used ONLY to strengthen the kill confirmation — "a leg is connected right now, killing this drops
    it". It must never gate the kill: an operator who cannot reach their own relay must still be able to
    tear a session down.

    Phase 1 has no relay to ask. The rendezvous is somebody else's sshd, so sshim cannot see who is
    dialled in, and answering ``False`` would be a lie of the worst kind — it would tell an operator
    "nothing is connected" while their foothold sits attached. So Phase 1 answers ``None`` (unknown) and
    the confirmation swears to nothing it cannot see.

    Phase 2 asks the host: ``host_hook('exclusive_container_state')`` returns the container's view of its
    own connections (core owns the container, so core answers).
    """
    probe = _extras().get("exclusive_container_state")
    if probe is None:
        return None
    try:
        state = probe(str(session.id))
    except Exception:  # noqa: BLE001 — a probe failure must not block a kill
        return None
    if isinstance(state, bool):
        return state
    if isinstance(state, dict):
        legs = state.get("connected_legs")
        if isinstance(legs, int):
            return legs > 0
        if isinstance(legs, bool):
            return legs
    return None
