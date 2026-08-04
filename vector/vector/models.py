"""Vector data model (SQLAlchemy 2.0 declarative).

A diagram is a single row holding the whole ``vector.attackpath/v1`` document as JSON text — the editing
granularity lives client-side, the server is a JSON store + HTML exporter. Tables are ``vector_``-prefixed
so they never collide with a host's tables when mounted in the shared database.

Ownership (``owner_id`` / ``created_by``) is soft attribution + an access scope: list/read/write are
scoped to the owner (admins see everything, incl. legacy NULL-owner rows) by the blueprint/API, mirroring
lotek's job tenancy posture. ``client_id`` / ``source_job_id`` are SOFT references (plain integers, no FK)
to whatever host tables exist — Vector may run standalone with no such tables at all.
"""

from __future__ import annotations

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from vector.db import Base, TimestampMixin


class Client(Base, TimestampMixin):
    """Minimal client/org. Used as the DEFAULT client model standalone; when mounted with a host
    ``client_model`` injected (lotek's ``Client``), diagrams reference the host's table instead and this
    stays empty. Aligns with lotek's minimal ``Client`` (id, name)."""

    __tablename__ = "vector_clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)


class Diagram(Base, TimestampMixin):
    """One attack-path diagram: name + the full model JSON."""

    __tablename__ = "vector_diagrams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    # Soft references (no FK): may point at Vector's own tables (standalone) or the host's (mounted).
    client_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Access scope + attribution. NULL owner = "legacy / no owner" — visible only to admins (see the
    # blueprint/API scoping), never guessed onto another user.
    owner_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Non-deletable seeded example(s) are marked builtin so the UI can offer "duplicate, don't edit".
    builtin: Mapped[bool] = mapped_column(default=False, index=True)
    # The whole vector.attackpath/v1 document, normalized before store.
    model_json: Mapped[str] = mapped_column(Text, default="{}")
