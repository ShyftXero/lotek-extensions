"""Vector data model (SQLAlchemy 2.0 declarative).

A diagram is a single row holding the whole ``vector.attackpath/v1`` document as JSON text — the editing
granularity lives client-side, the server is a JSON store + HTML exporter. Tables are ``vector_``-prefixed
so they never collide with a host's tables when mounted in the shared database.

Ownership (``owner_id`` / ``created_by``) is soft attribution + an access scope: list/read/write are
scoped to the owner (admins see everything, incl. legacy NULL-owner rows) by the blueprint/API, mirroring
lotek's job tenancy posture. ``client_id`` / ``owner_id`` / ``source_job_id`` are SOFT references (UUIDs,
no FK) to whatever host tables exist — Vector may run standalone with no such tables at all. They are
UUID-typed because lotek's core keys ``Client``/``User``/``Job`` on UUIDv7 (v2); an Integer column can't
hold a UUID and the mounted INSERT would fail ``cannot cast type uuid to integer``.
"""

from __future__ import annotations

import uuid

from sqlalchemy import String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from vector.db import Base, TimestampMixin


class Client(Base, TimestampMixin):
    """Minimal client/org. Used as the DEFAULT client model standalone; when mounted with a host
    ``client_model`` injected (lotek's ``Client``), diagrams reference the host's table instead and this
    stays empty. Aligns with lotek's minimal ``Client`` (id, name)."""

    __tablename__ = "vector_clients"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    name: Mapped[str] = mapped_column(String(255), unique=True)


class Diagram(Base, TimestampMixin):
    """One attack-path diagram: name + the full model JSON."""

    __tablename__ = "vector_diagrams"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    name: Mapped[str] = mapped_column(String(255), index=True)
    # Soft references (no FK): may point at Vector's own tables (standalone) or the host's (mounted).
    # UUID-typed to match lotek's core UUIDv7 keys — see the module docstring.
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    source_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    # Access scope + attribution. NULL owner = "legacy / no owner" — visible only to admins (see the
    # blueprint/API scoping), never guessed onto another user.
    owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Non-deletable seeded example(s) are marked builtin so the UI can offer "duplicate, don't edit".
    builtin: Mapped[bool] = mapped_column(default=False, index=True)
    # The whole vector.attackpath/v1 document, normalized before store.
    model_json: Mapped[str] = mapped_column(Text, default="{}")


class UserPref(Base, TimestampMixin):
    """One host user's PERSONAL Vector preferences (lotek-extensions#111 / lotek#485).

    The USER half of the settings split, and it lives HERE rather than in the host on purpose: a
    personal preference crosses no privilege boundary, so it needs no admin gate, no audit row and no
    host storage — it is an ordinary owner-scoped row, reached through Vector's own ⚙ cog. The ADMIN
    half (``[[settings]]`` in ``lotek-extension.toml``) is the mirror image: the host owns the form,
    the gate, the store and the audit, and Vector only reads it.

    ``owner_id`` is a SOFT reference (no FK) to the host user, UUID-typed for the same reason
    ``Diagram.owner_id`` is — lotek keys ``User`` on UUIDv7 and an Integer column cannot hold one.
    Unique, so this is a per-user singleton; a user with no row simply gets the defaults.

    NOTE: nothing here may become an authorization input. ``blueprint.visible_diagrams_stmt()`` is the
    IDOR guard and is deliberately untouched by these — a preference filters what is ALREADY visible.
    """

    __tablename__ = "vector_user_prefs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True, index=True)
    #: Hide the seeded read-only example(s) from MY diagram list. Off by default so a new user still
    #: discovers the example; once you've seen it, it is clutter forever.
    hide_builtin_diagrams: Mapped[bool] = mapped_column(default=False)
