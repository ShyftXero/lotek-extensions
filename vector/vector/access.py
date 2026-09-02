"""The ONE home for diagram tenancy — visibility + write authorization (lotek#585, INV-TENANCY-05).

Both surfaces route through here so the two cannot drift (CLAUDE.md "one derived-state predicate, one
home"): the cookie blueprint (``blueprint.py`` / ``api.py``, principal = the browser-session user) and the
PAT machine API (``api_pat.py``, principal = the bearer token). They differ only in how they resolve the
actor's ``(is_admin, owner_id)``; they pass it in, and the ENGAGEMENT predicates below ask the host about
whatever principal the current request carries, so the rule is identical on both.

The rule:
  * ``builtin`` — the seeded read-only example — is visible to everyone (no engagement, no tenant).
  * A diagram BOUND to an engagement (``engagement_id`` not NULL) is visible iff the current principal
    holds a LIVE membership on that engagement (``host_can_view_engagement``), and writable iff it holds a
    live OPERATOR capability (``host_can_operate_on``). ``owner_id`` is NOT consulted — that is the whole
    point: a member revoked from the engagement, the owner included, loses read/export/write.
  * An UNBOUND diagram (NULL engagement — standalone, legacy, or created without an engagement) has no
    engagement to check, so it keeps the older owner scope: visible/writable to its owner and to admins;
    a NULL-owner unbound row is admin-only (never guessed onto a user).
"""

from __future__ import annotations

import uuid

from sqlalchemy import and_, false, or_, select

from vector.deps import (
    host_can_operate_on,
    host_can_view_engagement,
    host_visible_engagement_ids,
)
from vector.models import Diagram


def diagram_visible(d: Diagram, *, is_admin: bool, owner_id: uuid.UUID | None) -> bool:
    """May the current request read/export this diagram? (Per-row; the authoritative single-object gate.)"""
    if d.builtin:
        return True
    if d.engagement_id is not None:
        return host_can_view_engagement(d.engagement_id)
    if is_admin:
        return True
    return owner_id is not None and d.owner_id == owner_id


def diagram_writable(d: Diagram, *, is_admin: bool, owner_id: uuid.UUID | None) -> bool:
    """May the current request mutate this diagram? ``builtin`` is read-only everywhere and is refused by
    the caller BEFORE this (a builtin has no engagement, so it would fall through to the owner arm); this
    answers the tenancy half only."""
    if d.engagement_id is not None:
        return host_can_operate_on(d.engagement_id)
    if is_admin:
        return True
    return owner_id is not None and d.owner_id == owner_id


def visible_diagrams_stmt(*, is_admin: bool, owner_id: uuid.UUID | None):
    """A ``select(Diagram)`` of what the current request may see — the LIST form of ``diagram_visible``.

    The engagement-bound rows are narrowed in SQL against the host's scoped id set; the unbound rows keep
    the owner scope. An empty/absent id set means no bound rows match (fail closed) — safe because an
    unmounted/standalone Vector has no bound rows.
    """
    eng_ids = host_visible_engagement_ids()
    bound = Diagram.engagement_id.in_(eng_ids) if eng_ids else false()
    if is_admin:
        unbound = Diagram.engagement_id.is_(None)
    elif owner_id is not None:
        unbound = and_(Diagram.engagement_id.is_(None), Diagram.owner_id == owner_id)
    else:
        unbound = false()
    return select(Diagram).where(or_(Diagram.builtin.is_(True), bound, unbound))
