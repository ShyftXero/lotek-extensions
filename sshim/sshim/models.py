"""sshim data model — two tables: a saved target (``sshim_profiles``) and a minted pivot
(``sshim_sessions``).

``owner_id`` is a **UUID soft reference** to a core ``User`` (no cross-schema FK; the core table is
not known until mount time, and sshim may run standalone). It is ``sqlalchemy.Uuid``, never
``Integer``/``String``: core v2 keys every surrogate PK on UUIDv7, and an Integer core-ref column is
INV-INTEGRITY-03's exact red path (``cannot cast type uuid to integer`` — it has taken production down).

Tenancy: neither table carries an ``engagement_id`` — a pivot is operator infrastructure, not
engagement data. INV-TENANCY-06 allows that only under an *explicit declared platform rule* enforced in
exactly one place (``sshim/service.py``): **a profile/session is visible to its owner, and to an admin,
and to nobody else, on any surface.**

**The session row stores enough to REGENERATE the plan, and nothing secret.** ``params`` is the JSON
snapshot of the minting ``plan.Profile`` (so a later profile edit/delete cannot change a minted
session's commands), plus the two random ports and the TTL. The relay password is NEVER persisted:
``plan.render_plan`` keeps it off every command by design, so the stored session re-renders to
byte-identical commands without it (``plan.rebuild_session``). The one-time secret is shown at mint and
then forgotten; the re-fetch surface says so and offers a re-mint rather than show a stale/empty one.

``status`` records only the explicit operator action — ``active`` or ``killed``. **``expired`` is
DERIVED** from ``expires_at`` by ``plan.effective_status`` and is never stored: a stored expiry flag
would be a second copy of the liveness fact that needs a sweeper and can drift.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sshim.db import Base, TimestampMixin, UuidPk

#: Bound the free-text fields server-side (the browser `maxlength` is a hint, not a control).
MAX_NAME = 120
MAX_HOST = 255
MAX_USER = 64
MAX_CIDR = 64


class Profile(Base, UuidPk, TimestampMixin):
    """A saved target — the rendezvous/anchor coordinates an operator mints against repeatedly.

    In Phase 1 every host here is operator-typed text (a VPS / tailnet node). Phase 2 (lotek#1747) will
    let a profile instead REQUEST a lotek-hosted single-use relay through the host seam
    (``exclusive_container_acquire``); that is a future column + an ``extras`` hook, deliberately not
    built here.
    """

    __tablename__ = "sshim_profiles"

    # Soft ref to a core User (UUIDv7). NULL only standalone, where there is no host identity.
    owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(MAX_NAME))
    topology: Mapped[str] = mapped_column(String(40), default="relay_reverse_socks")
    rendezvous_host: Mapped[str | None] = mapped_column(String(MAX_HOST), nullable=True)
    rendezvous_port: Mapped[int] = mapped_column(Integer, default=22)
    rendezvous_user: Mapped[str] = mapped_column(String(MAX_USER), default="sshim")
    anchor_host: Mapped[str | None] = mapped_column(String(MAX_HOST), nullable=True)
    anchor_port: Mapped[int] = mapped_column(Integer, default=22)
    anchor_user: Mapped[str] = mapped_column(String(MAX_USER), default="sshim")
    cidr: Mapped[str] = mapped_column(String(MAX_CIDR), default="")

    sessions: Mapped[list[Session]] = relationship(back_populates="profile")


class Session(Base, UuidPk, TimestampMixin):
    """One minted pivot — the re-fetchable unit. Carries everything needed to REGENERATE both legs'
    commands from ``plan.render_plan`` (a profile snapshot + the random ports + ttl) and nothing secret.
    """

    __tablename__ = "sshim_sessions"

    owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    # Soft link to the profile this was minted from. SET NULL (not CASCADE): deleting a profile must
    # NOT delete the running pivots minted from it — the session regenerates from its own `params`
    # snapshot, so it stays valid and re-fetchable after the profile is gone.
    profile_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("sshim_profiles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Denormalised at mint for the list view + stable display after the profile is edited/deleted.
    profile_name: Mapped[str] = mapped_column(String(MAX_NAME), default="")
    topology: Mapped[str] = mapped_column(String(40), default="relay_reverse_socks")
    rendezvous_host: Mapped[str | None] = mapped_column(String(MAX_HOST), nullable=True)
    socks_port: Mapped[int] = mapped_column(Integer, default=0)
    jump_port: Mapped[int] = mapped_column(Integer, default=0)
    ttl_minutes: Mapped[int] = mapped_column(Integer, default=30)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    # Explicit operator action ONLY: "active" | "killed". `expired` is DERIVED (plan.effective_status),
    # never stored — see the module docstring.
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    # The JSON snapshot of the minting plan.Profile — the regeneration source (plan.rebuild_session).
    params: Mapped[dict] = mapped_column(JSON, default=dict)

    profile: Mapped[Profile | None] = relationship(back_populates="sessions")
