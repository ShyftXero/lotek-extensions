"""Runtime accessors for blueprint views: the mounted config + a session context manager."""

from __future__ import annotations

from contextlib import contextmanager

from flask import current_app
from sqlalchemy import select

from scribble.config import ScribbleConfig


def get_config() -> ScribbleConfig:
    cfg = current_app.extensions.get("scribble")
    if cfg is None:  # pragma: no cover - misconfiguration guard
        raise RuntimeError("Scribble is not registered on this app (call scribble.register).")
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


def current_actor_username() -> str | None:
    """The logged-in host user's username, for attribution (``created_by`` columns), or ``None``.

    Reads the optional ``extras['current_actor']`` hook -- a ``() -> host_user_or_None`` callable the
    host injects on the mounted :class:`~scribble.config.ScribbleConfig` (Lotek: the session user; see
    docs/LOTEK_ADOPTION.md §4.4). Absent hook, no logged-in user, a user object with no ``.username``,
    or a raising hook all resolve to ``None`` -- standalone Scribble (no host) always gets ``None``, and
    this is an attribution nicety, never an authz gate, so a misbehaving hook must never break the write
    it decorates.
    """
    try:
        cfg = get_config()
    except RuntimeError:  # pragma: no cover - defensive; no app context
        return None
    hook = cfg.extras.get("current_actor")
    if hook is None:
        return None
    try:
        actor = hook()
    except Exception:  # noqa: BLE001 - a throwing host hook must not break the write it decorates
        return None
    if actor is None:
        return None
    return getattr(actor, "username", None)


def current_actor():
    """The logged-in host user OBJECT (or ``None``), for authorization checks that need its ``id``/``role``
    (e.g. scoping the report routes — audit CRIT-4). Same optional ``extras['current_actor']`` hook and
    fail-safe contract as :func:`current_actor_id`; standalone Scribble (no host) always gets ``None``."""
    try:
        cfg = get_config()
    except RuntimeError:  # pragma: no cover - defensive; no app context
        return None
    hook = cfg.extras.get("current_actor")
    if hook is None:
        return None
    try:
        return hook()
    except Exception:  # noqa: BLE001 - a throwing host hook must never break the read it guards
        return None


def current_actor_id() -> int | None:
    """The logged-in host user's id (for ``Engagement.owner_id`` attribution), or ``None``. Same
    optional ``extras['current_actor']`` hook + fail-safe contract as :func:`current_actor_username`
    (standalone Scribble always gets ``None``). Ownership is attribution + admin oversight, NEVER an
    access gate — engagements stay team-shared so live collaboration keeps working — so a misbehaving
    hook must never break the write it decorates.
    """
    try:
        cfg = get_config()
    except RuntimeError:  # pragma: no cover - defensive; no app context
        return None
    hook = cfg.extras.get("current_actor")
    if hook is None:
        return None
    try:
        actor = hook()
    except Exception:  # noqa: BLE001
        return None
    ident = getattr(actor, "id", None)
    return ident if isinstance(ident, int) else None


def client_model():
    """The ``Client`` model this mounted Scribble should query/write against.

    Returns the host-injected model (``ScribbleConfig.client_model``, e.g. Lotek's ``Client``) when one
    was passed to ``register()``; otherwise Scribble's own ``scribble.models.Client`` -- standalone
    Scribble (no host, or a host that didn't inject one) always gets its own table. This is what makes
    ``Engagement.client_id`` a soft reference rather than a hard FK (docs/LOTEK_ADOPTION.md §3.1): the
    id may belong to ``scribble_clients`` (standalone) or the host's own client table (mounted), and
    only this resolver -- not a static SQLAlchemy relationship -- can tell which at call time.
    """
    from scribble.models import Client  # local import: avoids a models.py <-> deps.py import cycle

    try:
        cfg = get_config()
    except RuntimeError:  # pragma: no cover - defensive; no app context
        return Client
    return cfg.client_model or Client


def severity_enum():
    """The ``Severity`` enum this mounted Scribble should construct/validate against.

    Returns the host-injected enum (``ScribbleConfig.severity_enum``, e.g. Lotek's ``Severity``) when
    one was passed to ``register()``; otherwise Scribble's own ``scribble.enums.Severity``. The two
    vocabularies are value-for-value identical (docs/LOTEK_ADOPTION.md §3.2), so swapping the enum
    object a boundary (e.g. ``EngagementFinding.from_lotek_finding``) constructs from a raw string never
    changes which string values are valid -- it only means "one severity vocabulary when mounted".
    """
    from scribble.enums import Severity  # local import: mirrors client_model's cycle-avoidance

    try:
        cfg = get_config()
    except RuntimeError:  # pragma: no cover - defensive; no app context
        return Severity
    return cfg.severity_enum or Severity


def client_names(session, engagements) -> dict[int, str]:
    """Bulk-resolve ``{engagement.id: display client name}`` in ONE query, for list views.

    ``Engagement.client_id`` is a soft reference (no FK, no static ``.client`` relationship -- see
    docs/LOTEK_ADOPTION.md §3.1), so list views can't lazy-load a client per row; this resolves through
    ``client_model()`` instead (Lotek's when mounted, Scribble's own ``scribble.models.Client``
    standalone). Missing/unresolvable client ids fall back to "—". Shared by ``scribble/blueprint.py``'s
    dashboard and ``scribble/engagement_ui.py``'s engagement list so the resolution logic lives once.
    """
    ids = {e.client_id for e in engagements if e.client_id is not None}
    if not ids:
        return {}
    ClientModel = client_model()
    rows = session.scalars(select(ClientModel).where(ClientModel.id.in_(ids))).all()
    names_by_id = {row.id: row.name for row in rows}
    return {e.id: names_by_id.get(e.client_id, "—") for e in engagements if e.client_id is not None}


def host_can_write() -> bool:
    """Whether the current user may mutate, per the optional ``extras['can_write']`` hook (``() ->
    bool``) the host injects on the mounted config.

    Absent hook or a raising hook both default to ``True`` -- standalone Scribble (no host) is always
    writable, and this only drives a UI nudge (hide/disable controls that would otherwise be refused);
    the host's own request-method/role gate is the real enforcement (docs/LOTEK_ADOPTION.md §4.1), so
    failing this open never opens a security hole.
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
